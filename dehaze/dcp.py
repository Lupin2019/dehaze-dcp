import numpy as np
import scipy as sc
from typing import List, Optional, Union, Tuple
import time
from skimage.transform import resize
from collections import namedtuple

DehazeOutput = namedtuple(
    "DehazeOutput",
    ["I", "dc", "mask", "A", "tilde_t", "t", "J", "D"]
)
# namedtuple._asdict()

def _rgb2gray(A):
    r, g, b = A[..., 0], A[..., 1], A[..., 2]
    return 0.2989 * r + 0.5870 * g + 0.1140 * b

def _expand_A_as_B(A, B, left=False):
    """
    make len(A.shape) = len(B.shape)
    """
    while len(A.shape) < len(B.shape):
        if left:
            A = A[np.newaxis, ...]
        else:
            A = A[..., np.newaxis]
    return A

def get_dark_channel(img: np.ndarray, patch_size: Tuple[int, int]=(15,15)) -> np.ndarray:
    if len(img.shape) == 3 and img.shape[-1] == 3:
        img_min = np.min(img, axis=-1)
    elif len(img.shape) == 2 or (len(img.shape) == 3 and img.shape[-1] == 1):
        img_min = img.copy()
    else:
        raise NotImplementedError
    
    # ## keep the same output shape
    # img_padding = np.pad(img_min, 
    #                  ((patch_size // 2, patch_size // 2),
    #                  (patch_size // 2, patch_size // 2)),
    #                  mode='edge')
    
    # ## window min filter
    # dc = np.empty_like(img_min)
    # for i, j in np.ndindex(img_min.shape):
    #     dc[i, j, ...] = np.min(img_padding[i:i+patch_size, j:j+patch_size, ...])
    
    return sc.ndimage.minimum_filter(img_min, patch_size, mode='nearest')

def get_mask(dc, top_ratio:float=1e-3) -> Union[float, np.ndarray]:
    """
    average of the top-intensity pixels
    """
    numpix = max(int(dc.shape[0] * dc.shape[1] * top_ratio), 1)
    
    dc_flatten = dc.flatten()
    indices = np.argsort(dc_flatten)[-numpix:]
    mask = np.full(dc_flatten.shape, False, dtype=bool)
    mask[indices] = True
    
    return np.reshape(mask, dc.shape)


def get_atmos_light(im, dc, top_ratio:float=1e-3) -> Union[float, np.ndarray]:
    """
    average of the top-intensity pixels
    """
    numpix = max(int(dc.shape[0] * dc.shape[1] * top_ratio), 1)
    
    dc_flatten = dc.flatten()
    indices = np.argsort(dc_flatten)[-numpix:]
    mask = np.full_like(dc_flatten, False)
    mask[indices] = True

    if len(im.shape) == 3:
        mask = np.reshape(mask, dc.shape)[:, :, np.newaxis]
    elif len(im.shape) == 2:
        mask = np.reshape(mask, dc.shape)
    else:
        raise NotImplementedError
    
    res = mask * im
    return np.sum(res, axis=(0,1)) / numpix

def get_tilde_t(im, A, omega=0.95, **kwarg):
    # while len(A.shape) < len(im.shape):
    #     A = A[np.newaxis, :]
    A = _expand_A_as_B(A, im, left=True)
    return 1 - omega * get_dark_channel(im / A, **kwarg)

def get_laplace_matting_matrix(I:np.ndarray, consts:np.ndarray=None, eps=1e-7, win_size:int=1):
    """
    The original version is offered by Levin matlab code
    """
    h, w, c = I.shape
    img_size = h * w
    neb_size = (win_size * 2 + 1) ** 2

    ## the verse of "mask"
    if consts is not None:
        consts = sc.ndimage.binary_erosion(consts, structure=np.ones((win_size * 2 + 1, win_size * 2 + 1)))
        tlen = np.sum(1 - consts[win_size:h-win_size, win_size:w-win_size]) * (neb_size ** 2)
    else:
        tlen = (h-2*win_size) * (w-2*win_size) * neb_size ** 2

    indsM = np.arange(0, img_size).reshape(h, w)
    row_inds = np.zeros(tlen, dtype=int)
    col_inds = np.zeros(tlen, dtype=int)
    vals = np.zeros(tlen)
    LEN = 0

    for j in range(win_size, w - win_size):
        for i in range(win_size, h - win_size):
            if consts is not None and consts[i, j]:
                continue

            win_inds = indsM[i - win_size:i + win_size + 1, j - win_size: j + win_size + 1].flatten()
            winI = I[i - win_size: i + win_size + 1, j - win_size: j + win_size + 1].reshape(neb_size, c)
            win_mu = np.mean(winI, axis=0).reshape(3, 1)
            win_var = np.linalg.inv(winI.T @ winI / neb_size - (win_mu@win_mu.T) + eps / neb_size * np.eye(c))
            winI = winI - np.tile(win_mu.T, (neb_size, 1))
            tvals = (1 + (winI @ win_var) @ winI.T) / neb_size

            row_inds[LEN:LEN + neb_size**2] = np.tile(win_inds, (neb_size, 1)).flatten()
            col_inds[LEN:LEN + neb_size**2] = np.repeat(win_inds, neb_size).flatten()
            vals[LEN:LEN + neb_size**2] = tvals.flatten()

            LEN += neb_size**2   

    vals = vals[:LEN]

    row_inds = row_inds[:LEN]
    col_inds = col_inds[:LEN]

    A = sc.sparse.coo_matrix((vals, (row_inds, col_inds)), shape=(img_size, img_size))
    
    sumA = np.array(np.sum(A, axis=1)).squeeze()

    return sc.sparse.diags(sumA, 0, (img_size, img_size)) - A



def soft_matting(
    I:np.ndarray, p, lam=1e-4, **kwargs
):
    L = get_laplace_matting_matrix(I=I, **kwargs)
    # t = sc.sparse.linalg.spsolve(L + lam * sc.sparse.diags([1] * L.shape[0], 0), lam * p.flatten())
    t, info = sc.sparse.linalg.cg(L + lam * sc.sparse.diags([1] * L.shape[0], 0), lam * p.flatten())
    return t.reshape(p.shape)

def get_t(L, tilde_t, lam=1e-4, ):
    t = sc.sparse.linalg.spsolve(L + lam * sc.sparse.diags([1] * L.shape[0], 0), lam * tilde_t.flatten())
    return t.reshape(tilde_t.shape)

def get_J(I, A, t, t0=0.1, clip=True):
    A = _expand_A_as_B(A, I, left=True)
    t = np.clip(t, a_min=t0, a_max=1)
    t = _expand_A_as_B(t, I)
    res = (I - A) / t + A
    if clip:
        res = np.clip(res, a_min=0, a_max=1)
    return res

def get_depth(t, beta=0.388):
    return  - np.log(t) / beta


def guided_filter(I, p, ks:Tuple[int, int]=(41,41), eps=1e-3):
    if len(I.shape) == 3 and I.shape[-1] == 3:
        I = _rgb2gray(I)

    filter_mean = np.ones(ks)
    filter_mean /= np.sum(filter_mean)
    
    p = _expand_A_as_B(p, I)
    filter_mean = _expand_A_as_B(filter_mean, I)

    mean_I = sc.ndimage.convolve(I, filter_mean, mode="nearest")
    mean_p = sc.ndimage.convolve(p, filter_mean, mode="nearest")
    corr_Ip = sc.ndimage.convolve(I * p , filter_mean, mode="nearest")
    corr_I = sc.ndimage.convolve(I * I , filter_mean, mode="nearest")

    var_I = corr_I - mean_I * mean_I
    cov_Ip = corr_Ip - mean_I * mean_p

    a = cov_Ip / (var_I + eps)
    b = mean_p - a * mean_I

    mean_a = sc.ndimage.convolve(a, filter_mean, mode="nearest")
    mean_b = sc.ndimage.convolve(b, filter_mean, mode="nearest")

    res = mean_a * I + mean_b

    return res


def fast_guided_filter(I, p, ks:Tuple[int, int]=(41,41), eps=1e-3, s=4):
    if len(I.shape) == 3 and I.shape[-1] == 3:
        I = _rgb2gray(I)
    
    h, w = I.shape
    I0 = I.copy()

    I = resize(I, (h // s, w // s)) 
    p = _expand_A_as_B(p, I) # not useful
    p = resize(p, (h // s, w // s))

    # according to r, instead of the 2r + 1
    r0 = (ks[0] - 1) // 2
    r1 = (ks[1] - 1) // 2

    ks = (2 * r0 // s + 1, 2 * r1 // s + 1)

    filter_mean = np.ones(ks)
    filter_mean /= np.sum(filter_mean)
    
    
    filter_mean = _expand_A_as_B(filter_mean, I)

    mean_I = sc.ndimage.convolve(I, filter_mean, mode="nearest")
    mean_p = sc.ndimage.convolve(p, filter_mean, mode="nearest")
    corr_Ip = sc.ndimage.convolve(I * p , filter_mean, mode="nearest")
    corr_I = sc.ndimage.convolve(I * I , filter_mean, mode="nearest")

    var_I = corr_I - mean_I * mean_I
    cov_Ip = corr_Ip - mean_I * mean_p

    a = cov_Ip / (var_I + eps)
    b = mean_p - a * mean_I

    mean_a = sc.ndimage.convolve(a, filter_mean, mode="nearest")
    mean_b = sc.ndimage.convolve(b, filter_mean, mode="nearest")

    mean_a = resize(mean_a, (h, w))
    mean_b = resize(mean_b, (h, w))

    res = mean_a * I0 + mean_b
    return res


def dehaze_image(
    I: np.ndarray, 
    method: Optional[str],
    patch_size: Tuple[int, int] = (15,15), 
    top_ratio=1e-3,
    **kwargs,
):
    if method is not None and method not in ["soft", "guided", "fast"]:
        raise NotImplementedError(f"method {method} not NotImplemented")
    
    dc = get_dark_channel(I, patch_size)
    mask = get_mask(dc, top_ratio)
    A = get_atmos_light(I, dc, top_ratio)
    tilde_t = get_tilde_t(I, A)

    if method is None or method == "soft":
        t = soft_matting(I, p=tilde_t, **kwargs)
    elif method == "guided":
        t = guided_filter(I, p=tilde_t,  **kwargs)
    elif method == "fast":
        t = fast_guided_filter(I, p=tilde_t,  **kwargs)
    else:
        raise NotImplementedError
    
    J = get_J(I, A, t)
    D = get_depth(t)

    return DehazeOutput(I, dc, mask, A, tilde_t, t, J, D)

