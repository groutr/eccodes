#!/usr/bin/env python
from __future__ import print_function
import os
import re
import sys
import binascii
import time

assert len(sys.argv) > 2

start = time.time()
print("MEMFS: starting")

# Exclude experimental features e.g. GRIB3 and TAF
# The BUFR codetables is not used in the engine
EXCLUDED = ["grib3", "codetables", "taf", "stations"]

pos = 1
if sys.argv[1] == "-exclude":
    product = sys.argv[2]
    if product == "bufr":
        EXCLUDED.append(product)
    elif product == "grib":
        EXCLUDED.extend(["grib1", "grib2"])
    else:
        assert False, "Invalid product %s" % product
    pos = 3

dirs = [os.path.realpath(x) for x in sys.argv[pos:-1]]
print("Directories: ", dirs)
print("Excluding: ", EXCLUDED)

FILES = {}
SIZES = {}
NAMES = []
CHUNK = 16 * 1024 * 1024  # chunk size in bytes

# Binary to ASCII function. Different in Python 2 and 3
try:
    str(b"\x23\x20", "ascii")
    ascii = lambda x: str(x, "ascii")  # Python 3
except:
    ascii = lambda x: str(x)  # Python 2


def get_outfile_name(base, count):
    return base + "_" + str(count).zfill(3) + ".c"


# The last argument is the base name of the generated C file(s)
output_file_base = sys.argv[-1]

buffer = None
fcount = -1

for directory in dirs:

    # print("MEMFS: directory=", directory)
    dname = os.path.basename(directory)
    NAMES.append(dname)

    for dirpath, dirnames, files in os.walk(directory, followlinks=True):

        # Prune the walk by modifying the dirnames in-place
        dirnames[:] = [dirname for dirname in dirnames if dirname not in EXCLUDED]
        for name in files:

            if buffer is None:
                fcount += 1
                opath = get_outfile_name(output_file_base, fcount)
                print("MEMFS: Generating output:", opath)
                buffer = open(opath, "w")

            full = "%s/%s" % (dirpath, name)
            _, ext = os.path.splitext(full)
            if ext not in [".def", ".table", ".tmpl", ".list", ".txt"]:
                continue
            if name == "CMakeLists.txt":
                continue

            full = full.replace("\\", "/")
            fname = full[full.find("/%s/" % (dname,)) :]
            # print("MEMFS: Add ", fname)
            name = re.sub(r"\W", "_", fname)

            assert name not in FILES
            assert name not in SIZES
            FILES[name] = fname
            SIZES[name] = os.path.getsize(full)

            txt = "const unsigned char %s[] = {" % (name,)
            buffer.write(txt)

            with open(full, "rb") as f:
                i = 0
                # Python 2
                # contents_hex = f.read().encode("hex")

                # Python 2 and 3
                contents_hex = binascii.hexlify(f.read())

                # Read two characters at a time and convert to C hex
                # e.g. 23 -> 0x23
                for n in range(0, len(contents_hex), 2):
                    twoChars = ascii(contents_hex[n : n + 2])
                    txt = "0x%s," % (twoChars,)
                    buffer.write(txt)
                    i += 1
                    if (i % 20) == 0:
                        buffer.write("\n")

            buffer.write("};\n")
            if buffer.tell() >= CHUNK:
                buffer.close()
                buffer = None


if buffer is not None:
    buffer.close()

# The number of generated C files is hard coded.
# See memfs/CMakeLists.txt
assert fcount == 6, fcount
opath = output_file_base + "_final.c"
print("MEMFS: Generating output: ", opath)
g = open(opath, "w")

print(
    """
#include "eccodes_config.h"
#ifdef ECCODES_HAVE_FMEMOPEN
#define _GNU_SOURCE
#endif

#include <stdio.h>
#include <stdlib.h>
#include <errno.h>
#include <string.h>
#include "eccodes_windef.h"
""",
    file=g,
)

# Write extern variables with sizes
for k, v in SIZES.items():
    print("extern const unsigned char %s[%d];" % (k, v), file=g)

print(
    """
struct entry {
    const char* path;
    const unsigned char* content;
    size_t length;
} entries[] = { """,
    file=g,
)

items = [(v, k) for k, v in FILES.items()]

for k, v in sorted(items):
    print('{"/MEMFS%s", &%s[0], sizeof(%s) / sizeof(%s[0]) },' % (k, v, v, v), file=g)


print(
    """};

#if defined(ECCODES_HAVE_FUNOPEN) && !defined(ECCODES_HAVE_FMEMOPEN)

typedef struct mem_file {
    const char* buffer;
    size_t size;
    size_t pos;
} mem_file;

static int read_mem(void *data, char * buf, int len) {
    mem_file* f = (mem_file*)data;
    int n = len;

    if(f->pos + n > f->size) {
        n = f->size - f->pos;
    }

    memcpy(buf, f->buffer + f->pos, n);

    f->pos += n;
    return n;
}

static int write_mem(void* data, const char* buf, int len) {
    mem_file* f = (mem_file*)data;
    return -1;
}

static fpos_t seek_mem(void *data, fpos_t pos, int whence) {
    mem_file* f = (mem_file*)data;
    long newpos = 0;

    switch (whence) {
    case SEEK_SET:
        newpos = (long)pos;
        break;

    case SEEK_CUR:
        newpos = (long)f->pos + (long)pos;
        break;

    case SEEK_END:
        newpos = (long)f->size -  (long)pos;
        break;

    default:
        return -1;
        break;
  }

  if(newpos < 0) { newpos = 0; }
  if(newpos > f->size) { newpos = f->size; }

  f->pos = newpos;
  return newpos;
}

static int close_mem(void *data) {
    mem_file* f = (mem_file*)data;
    free(f);
    return 0;
}

static FILE* fmemopen(const char* buffer, size_t size, const char* mode){
    mem_file* f = (mem_file*)calloc(sizeof(mem_file), 1);
    if(!f) return NULL;

    f->buffer = buffer;
    f->size = size;

    return funopen(f, &read_mem, &write_mem, &seek_mem, &close_mem);
}

#elif defined(ECCODES_ON_WINDOWS)

#include <io.h>
#include <fcntl.h>
#include <windows.h>

static FILE *fmemopen(void* buffer, size_t size, const char* mode) {
    char path[MAX_PATH - 13];
    if (!GetTempPath(sizeof(path), path))
        return NULL;

    char filename[MAX_PATH + 1];
    if (!GetTempFileName(path, "eccodes", 0, filename))
        return NULL;

    HANDLE h = CreateFile(filename,
                          GENERIC_READ | GENERIC_WRITE,
                          0,
                          NULL,
                          OPEN_ALWAYS,
                          FILE_ATTRIBUTE_TEMPORARY | FILE_FLAG_DELETE_ON_CLOSE,
                          NULL);

    if (h == INVALID_HANDLE_VALUE)
        return NULL;

    int fd = _open_osfhandle((intptr_t)h, _O_RDWR);
    if (fd < 0) {
        CloseHandle(h);
        return NULL;
    }

    FILE* f = _fdopen(fd, "w+");
    if (!f) {
        _close(fd);
        return NULL;
    }

    fwrite(buffer, size, 1, f);
    rewind(f);
    return f;
}

#endif

static size_t entries_count = sizeof(entries)/sizeof(entries[0]);

static const unsigned char* find(const char* path, size_t* length) {
    size_t i;

    for(i = 0; i < entries_count; i++) {
        if(strcmp(path, entries[i].path) == 0) {
            /*printf("Found in MEMFS %s\\n", path);*/
            *length = entries[i].length;
            return entries[i].content;
        }
    }

    return NULL;
}

int codes_memfs_exists(const char* path) {
    size_t dummy;
    return find(path, &dummy) != NULL;
}

FILE* codes_memfs_open(const char* path) {
    size_t size;
    const unsigned char* mem = find(path, &size);
    if(!mem) {
        return NULL;
    }
    return fmemopen((void*)mem, size, "r");
}

""",
    file=g,
)

print("Finished")

print("MEMFS: done", time.time() - start)
