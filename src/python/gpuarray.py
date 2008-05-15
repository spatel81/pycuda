from __future__ import division
import numpy
from pytools import memoize
import pycuda.driver as drv



def splay(n, min_threads, max_threads, max_blocks):
    # stolen from cublas

    if n < min_threads:
        block_count = 1
        elems_per_block = n
        threads_per_block = min_threads
    elif n < (max_blocks * min_threads):
        block_count = (n + min_threads - 1) // min_threads
        threads_per_block = min_threads
        elems_per_block = threads_per_block
    elif n < (max_blocks * max_threads):
        block_count = max_blocks
        grp = (n + min_threads - 1) // min_threads
        threads_per_block = ((grp + max_blocks -1) // max_blocks) * min_threads
        elems_per_block = threads_per_block
    else:
        block_count = max_blocks
        threads_per_block = max_threads
        grp = (n + min_threads - 1) // min_threads
        grp = (grp + max_blocks - 1) // max_blocks
        elems_per_block = grp * min_threads

    #print "bc:%d tpb:%d epb:%d" % (block_count, threads_per_block, elems_per_block)
    return block_count, threads_per_block, elems_per_block






@memoize
def get_axpbyz_kernel():
    mod = drv.SourceModule("""
        __global__ void axpbyz(float a, float *x, float b, float *y, float *z,
          int n)
        {
          int tid = threadIdx.x;
          int total_threads = gridDim.x*blockDim.x;
          int cta_start = blockDim.x*blockIdx.x;
          int i;
                
          for (i = cta_start + tid; i < n; i += total_threads) 
          {
            z[i] = a*x[i] + b*y[i];
          }
        }
        """)

    return mod.get_function("axpbyz")




@memoize
def get_scale_kernel():
    mod = drv.SourceModule("""
        __global__ void scale(float a, float *x, float *y,int n)
        {
          int tid = threadIdx.x;
          int total_threads = gridDim.x*blockDim.x;
          int cta_start = blockDim.x*blockIdx.x;
          int i;
                
          for (i = cta_start + tid; i < n; i += total_threads) 
          {
            y[i] = a*x[i];
          }
        }
        """)

    return mod.get_function("scale")




@memoize
def get_fill_kernel():
    mod = drv.SourceModule("""
        __global__ void fill(float a, float *x, int n)
        {
          int tid = threadIdx.x;
          int total_threads = gridDim.x*blockDim.x;
          int cta_start = blockDim.x*blockIdx.x;
          int i;
                
          for (i = cta_start + tid; i < n; i += total_threads) 
          {
            x[i] = a;
          }
        }
        """)

    return mod.get_function("fill")




WARP_SIZE = 32




class GPUArray(object):
    def __init__(self, shape, dtype, stream=None):
        self.shape = shape
        self.dtype = numpy.dtype(dtype)
        from pytools import product
        self.size = product(shape)
        self.gpudata = drv.mem_alloc(self.size * self.dtype.itemsize)
        self.stream = stream

    def set(self, ary, stream=None):
        assert ary.size == self.size
        assert ary.dtype == self.dtype
        drv.memcpy_htod(self.gpudata, ary, stream)

    def get(self, ary=None, stream=None, pagelocked=False):
        if ary is None:
            if pagelocked:
                ary = drv.pagelocked_empty(self.shape, self.dtype)
            else:
                ary = numpy.empty(self.shape, self.dtype)
        else:
            assert ary.size == self.size
            assert ary.dtype == self.dtype
        drv.memcpy_dtoh(ary, self.gpudata)
        return ary

    def __str__(self):
        return str(self.get())

    def __repr__(self):
        return repr(self.get())

    def _axpbyz(self, selffac, other, otherfac, out):
        assert self.dtype == numpy.float32
        assert self.shape == other.shape
        assert self.dtype == other.dtype

        if self.stream is not None or other.stream is not None:
            assert self.stream is other.stream

        block_count, threads_per_block, elems_per_block = splay(self.size, WARP_SIZE, 128, 80)

        get_axpbyz_kernel()(numpy.float32(selffac), self.gpudata, 
                numpy.float32(otherfac), other.gpudata, 
                out.gpudata, numpy.int32(self.size),
                shared=256, block=(threads_per_block,1,1), grid=(block_count,1),
                stream=self.stream)

        return out

    def __add__(self, other):
        result = GPUArray(self.shape, self.dtype)
        return self._axpbyz(1, other, 1, result)

    def __sub__(self, other):
        result = GPUArray(self.shape, self.dtype)
        return self._axpbyz(1, other, -1, result)

    def _scale(self, factor):
        assert self.dtype == numpy.float32

        block_count, threads_per_block, elems_per_block = splay(self.size, WARP_SIZE, 128, 80)

        result = GPUArray(self.shape, self.dtype)
        get_scale_kernel()(numpy.float32(factor), self.gpudata, 
                result.gpudata, numpy.int32(self.size),
                shared=256, block=(threads_per_block,1,1), grid=(block_count,1),
                stream=self.stream)

        return result

    def __neg__(self):
        return self._scale(-1)

    def __mul__(self, scalar):
        return self._scale(scalar)

    def __rmul__(self, scalar):
        return self._scale(scalar)

    def fill(self, value):
        assert self.dtype == numpy.float32

        block_count, threads_per_block, elems_per_block = splay(self.size, WARP_SIZE, 128, 80)

        result = GPUArray(self.shape, self.dtype)
        get_fill_kernel()(numpy.float32(value), self.gpudata, numpy.int32(self.size),
                shared=256, block=(threads_per_block,1,1), grid=(block_count,1),
                stream=self.stream)

        return result




def to_gpu(ary, stream=None):
    result = GPUArray(ary.shape, ary.dtype)
    result.set(ary, stream)
    return result




empty = GPUArray

def zeros(shape, dtype, stream=None):
    result = GPUArray(shape, dtype, stream)
    result.fill(0)
    return result