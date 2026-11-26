"""Initialize the shipped ggml Vulkan backend in a disposable process."""
import ctypes as C
import json
import os
import sys


def probe(library):
    os.environ.pop('GGML_VK_VISIBLE_DEVICES', None)
    backend = C.CDLL(library)
    device_count = backend.ggml_backend_vk_get_device_count
    device_count.argtypes = ()
    device_count.restype = C.c_int
    if device_count() <= 0:
        return None
    describe = backend.ggml_backend_vk_get_device_description
    describe.argtypes = (C.c_int, C.c_char_p, C.c_size_t)
    describe.restype = None
    name = C.create_string_buffer(256)
    describe(0, name, len(name))
    initialize = backend.ggml_backend_vk_init
    initialize.argtypes = (C.c_size_t,)
    initialize.restype = C.c_void_p
    handle = initialize(0)
    if not handle:
        raise RuntimeError('Vulkan initialization returned no backend')
    free = backend.ggml_backend_free
    free.argtypes = (C.c_void_p,)
    free.restype = None
    free(handle)
    return name.value.decode('utf-8', 'replace')


if __name__ == '__main__':
    print(json.dumps({'device': probe(sys.argv[1])}))
