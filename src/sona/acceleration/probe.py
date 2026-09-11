"""Native checks run only in disposable children, never in the GUI process."""

import ctypes as C
import logging
import multiprocessing
import os
import platform
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path

from ..logging_config import configure_logging
from .catalog import REQUIRED_DLLS

logger = logging.getLogger(__name__)
_handles = []  # DLL-directory cookies and loaded modules must outlive inference.
P, I, S = C.c_void_p, C.c_int, C.c_size_t


class NativeError(RuntimeError):
    def __init__(self, name, code):
        self.driver = (name.startswith(('cuInit', 'cuda', 'cuDevice')) and code in (35, 222, 803, 804))
        super().__init__(f'{name} failed: code={code}')


def function(library, name, types):
    call = getattr(library, name)
    call.argtypes = types
    call.restype = I

    def checked(*args):
        code = call(*args)
        if code:
            raise NativeError(name, code)
    return checked


def hardware():
    if sys.platform == 'darwin':
        return {'kind': 'apple' if platform.machine().lower() == 'arm64' else 'unsupported'}
    if sys.platform != 'win32' or platform.machine().lower() not in ('amd64', 'x86_64'):
        return {'kind': 'cpu'}
    system = C.create_unicode_buffer(32768)
    if not C.windll.kernel32.GetSystemDirectoryW(system, len(system)):
        raise OSError('GetSystemDirectoryW failed')
    path = Path(system.value) / 'nvcuda.dll'
    if not path.is_file():
        # An absent driver DLL cannot prove that no physical GPU exists.
        return {'kind': 'cpu'}
    driver = C.WinDLL(str(path))
    try:
        function(driver, 'cuInit', [C.c_uint])(0)
    except NativeError as error:
        if 'code=100' in str(error):
            return {'kind': 'cpu'}
        raise
    count, version = I(), I()
    function(driver, 'cuDeviceGetCount', [P])(C.byref(count))
    function(driver, 'cuDriverGetVersion', [P])(C.byref(version))
    for index in range(count.value):
        device, major, minor = I(), I(), I()
        function(driver, 'cuDeviceGet', [P, I])(C.byref(device), index)
        name = C.create_string_buffer(256)
        function(driver, 'cuDeviceGetName', [P, I, I])(name, len(name), device)
        function(driver, 'cuDeviceComputeCapability', [P, P, I])(C.byref(major), C.byref(minor), device)
        logger.info('显卡检测 index=%d name=%s compute=%d.%d driver_api=%d',
                    index, name.value.decode(errors='replace'), major.value, minor.value, version.value)
        if major.value >= 5:
            return {'kind': 'nvidia', 'device_index': index, 'name': name.value.decode(errors='replace'),
                    'driver': version.value, 'driver_required': version.value < 12000}
    return {'kind': 'cpu'}


def bootstrap(path):
    """Before importing CTranslate2, prefer our complete, matching DLL family."""
    root = Path(path).resolve(strict=True)
    if sys.platform != 'win32':
        raise RuntimeError('Windows acceleration bundle used on another platform')
    _handles.append(os.add_dll_directory(str(root)))
    os.environ['PATH'] = str(root) + os.pathsep + os.environ.get('PATH', '')
    ordered = ['cudart64_12.dll', 'cublasLt64_12.dll', 'cublas64_12.dll']
    ordered += [p.name for p in sorted(root.glob('nvrtc-builtins*.dll'))]
    ordered += [name for name in REQUIRED_DLLS if name not in ordered]
    for name in ordered:
        _handles.append(C.CDLL(str(root / name)))
    logger.info('已加载应用加速运行库 path=%s', root)


def compute(path, index):
    bootstrap(path)
    # Import in the same order as the real worker, to catch loader conflicts.
    import ctranslate2
    logger.info('识别引擎显卡精度 types=%s', ctranslate2.get_supported_compute_types('cuda', index))
    root = Path(path)
    cuda = C.CDLL(str(root / 'cudart64_12.dll'))
    blas = C.CDLL(str(root / 'cublas64_12.dll'))
    # Legacy convolution symbols reside in the cnn and ops sublibraries in v9.
    cnn = C.CDLL(str(root / 'cudnn_cnn64_9.dll'))
    ops = C.CDLL(str(root / 'cudnn_ops64_9.dll'))
    graph = C.CDLL(str(root / 'cudnn_graph64_9.dll'))
    function(cuda, 'cudaSetDevice', [I])(index)
    copy = function(cuda, 'cudaMemcpy', [P, P, S, I])
    sync = function(cuda, 'cudaDeviceSynchronize', [])
    with ExitStack() as cleanup:
        def allocate(size):
            pointer = P()
            function(cuda, 'cudaMalloc', [P, S])(C.byref(pointer), size)
            cleanup.callback(function(cuda, 'cudaFree', [P]), pointer)
            return pointer

        def descriptor(lib, create, destroy):
            value = P()
            function(lib, create, [P])(C.byref(value))
            cleanup.callback(function(lib, destroy, [P]), value)
            return value

        x, w, y = allocate(16), allocate(4), allocate(16)
        values, weight = (C.c_float * 4)(1, 2, 3, 4), C.c_float(2)
        copy(x, values, 16, 1)
        copy(w, C.byref(weight), 4, 1)
        alpha, beta = C.c_float(1), C.c_float(0)
        handle = descriptor(blas, 'cublasCreate_v2', 'cublasDestroy_v2')
        function(blas, 'cublasSgemm_v2', [P, I, I, I, I, I, P, P, I, P, I, P, P, I])(
            handle, 0, 0, 4, 1, 1, C.byref(alpha), x, 4, w, 1, C.byref(beta), y, 4)
        sync()
        result = (C.c_float * 4)()
        copy(result, y, 16, 2)
        if list(result) != [2, 4, 6, 8]:
            raise RuntimeError('cuBLAS computation returned incorrect values')
        logger.info('加速验证 cuBLAS 矩阵运算通过')
        handle = descriptor(graph, 'cudnnCreate', 'cudnnDestroy')
        xd = descriptor(ops, 'cudnnCreateTensorDescriptor', 'cudnnDestroyTensorDescriptor')
        yd = descriptor(ops, 'cudnnCreateTensorDescriptor', 'cudnnDestroyTensorDescriptor')
        wd = descriptor(ops, 'cudnnCreateFilterDescriptor', 'cudnnDestroyFilterDescriptor')
        cd = descriptor(cnn, 'cudnnCreateConvolutionDescriptor', 'cudnnDestroyConvolutionDescriptor')
        set_tensor = function(ops, 'cudnnSetTensor4dDescriptor', [P, I, I, I, I, I, I])
        set_tensor(xd, 0, 0, 1, 1, 2, 2)
        set_tensor(yd, 0, 0, 1, 1, 2, 2)
        function(ops, 'cudnnSetFilter4dDescriptor', [P, I, I, I, I, I, I])(wd, 0, 0, 1, 1, 1, 1)
        function(cnn, 'cudnnSetConvolution2dDescriptor', [P, I, I, I, I, I, I, I, I])(
            cd, 0, 0, 1, 1, 1, 1, 1, 0)
        workspace_size = S()
        function(cnn, 'cudnnGetConvolutionForwardWorkspaceSize', [P, P, P, P, P, I, P])(
            handle, xd, wd, cd, yd, 0, C.byref(workspace_size))
        workspace = allocate(workspace_size.value) if workspace_size.value else None
        function(cuda, 'cudaMemset', [P, I, S])(y, 0, 16)
        function(cnn, 'cudnnConvolutionForward', [P, P, P, P, P, P, P, I, P, S, P, P, P])(
            handle, C.byref(alpha), xd, x, wd, w, cd, 0, workspace, workspace_size,
            C.byref(beta), yd, y)
        sync()
        copy(result, y, 16, 2)
        if list(result) != [2, 4, 6, 8]:
            raise RuntimeError('cuDNN computation returned incorrect values')
        logger.info('加速验证 cuDNN 卷积运算通过')


def run_probe(send, path=None, index=0):
    configure_logging('acceleration')
    def watch_parent():
        parent = multiprocessing.parent_process()
        while parent is not None:
            if not parent.is_alive():
                os._exit(1)
            time.sleep(1)
    threading.Thread(target=watch_parent, daemon=True).start()
    try:
        if path:
            compute(path, index)
            result = {'ok': True}
        else:
            result = {'ok': True, 'hardware': hardware()}
        send.send(result)
    except Exception as error:
        logger.exception('加速检查失败')
        send.send({'ok': False, 'driver_required': getattr(error, 'driver', False),
                   'error': f'{type(error).__name__}: {error}'})
    finally:
        send.close()
