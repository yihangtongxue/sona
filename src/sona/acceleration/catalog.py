"""Pinned NVIDIA redistributables. No manifest requests at application startup.

Sources: compute/cuda/redist/redistrib_12.8.1.json and
compute/cudnn/redist/redistrib_9.8.0.json on developer.download.nvidia.com.
"""

BUNDLE = 'cuda12.8-cudnn9.8-v1'
BASE = 'https://developer.download.nvidia.com/compute/'
ARCHIVES = (
    ('cudart', 'cuda/redist/cuda_cudart/windows-x86_64/cuda_cudart-windows-x86_64-12.8.90-archive.zip',
     3037735, '4a39058fd8519444a81cfc7ae055d136f48d1a31ffa41ae255b35b2edd61e13b'),
    ('cublas', 'cuda/redist/libcublas/windows-x86_64/libcublas-windows-x86_64-12.8.4.1-archive.zip',
     563660944, '57a470112cec7e112c95253dde8b3c7184d795dbd92b0bde77a4cb7f8c94c8aa'),
    ('nvrtc', 'cuda/redist/cuda_nvrtc/windows-x86_64/cuda_nvrtc-windows-x86_64-12.8.93-archive.zip',
     305588898, 'a63302a077f0248a743a1a7caa7dbd80d0fac56c6cfa9c41fa05fac9b7e5eda5'),
    ('cudnn', 'cudnn/redist/cudnn/windows-x86_64/cudnn-windows-x86_64-9.8.0.87_cuda12-archive.zip',
     675349654, 'd8a23705e3884b137b7e05449fb2b61bfa524e7cfc3fda80743d633f423c6ce4'),
)
TOTAL_BYTES = sum(item[2] for item in ARCHIVES)
REQUIRED_DLLS = (
    'cudart64_12.dll', 'cublasLt64_12.dll', 'cublas64_12.dll',
    'nvrtc64_120_0.dll', 'cudnn64_9.dll', 'cudnn_ops64_9.dll',
    'cudnn_cnn64_9.dll', 'cudnn_adv64_9.dll', 'cudnn_graph64_9.dll',
    'cudnn_engines_precompiled64_9.dll', 'cudnn_engines_runtime_compiled64_9.dll',
    'cudnn_heuristic64_9.dll',
)
