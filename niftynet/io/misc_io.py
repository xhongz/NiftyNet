# -*- coding: utf-8 -*-
from __future__ import absolute_import, print_function

import os
import warnings

import nibabel as nib
import numpy as np
import scipy.ndimage
import tensorflow as tf

image_loaders = [nib.load]
try:
    import niftynet.io.simple_itk_as_nibabel

    image_loaders.append(
        niftynet.io.simple_itk_as_nibabel.SimpleITKAsNibabel)
except ImportError:
    warnings.warn(
        'SimpleITK adapter failed to load, reducing the supported file formats.',
        ImportWarning)

warnings.simplefilter("ignore", UserWarning)

FILE_EXTENSIONS = [".nii.gz", ".tar.gz"]


#### utilities for file headers

def infer_ndims_from_file(file_path):
    image_header = load_image(file_path).header
    return int(image_header['dim'][0])


def create_affine_pixdim(affine, pixdim):
    '''
    Given an existing affine transformation and the pixel dimension to apply,
    create a new affine matrix that satisfies the new pixel dimension
    :param affine: original affine matrix
    :param pixdim: pixel dimensions to apply
    :return:
    '''
    norm_affine = np.sqrt(np.sum(np.square(affine[:, 0:3]), 0))
    to_divide = np.tile(
        np.expand_dims(np.append(norm_affine, 1), axis=1), [1, 4])
    to_multiply = np.tile(
        np.expand_dims(np.append(np.asarray(pixdim), 1), axis=1), [1, 4])
    return np.multiply(np.divide(affine, to_divide.T), to_multiply.T)


def load_image(filename):
    # load an image from a supported filetype and return an object
    # that matches nibabel's spatialimages interface
    for image_loader in image_loaders:
        try:
            img = image_loader(filename)
            img = correct_image_if_necessary(img)
            return img
        except nib.filebasedimages.ImageFileError:
            # if the image_loader cannot handle the type continue to next loader
            pass
    raise nib.filebasedimages.ImageFileError(
        'No loader could load the file')  # Throw last error


def correct_image_if_necessary(img):
    if img.header['dim'][0] == 5:
        # do nothing for high-dimensional array
        return img
    # Check that affine matches zooms
    pixdim = img.header.get_zooms()
    if not np.array_equal(np.sqrt(np.sum(np.square(img.affine[0:3, 0:3]), 0)),
                          np.asarray(pixdim)):
        if hasattr(img, 'get_sform'):
            # assume it is a malformed NIfTI and try to fix it
            img = rectify_header_sform_qform(img)
    return img


def rectify_header_sform_qform(img_nii):
    '''
    Look at the sform and qform of the nifti object and correct it if any
    incompatibilities with pixel dimensions
    :param img_nii:
    :return:
    '''
    # TODO: check img_nii is a nibabel object
    pixdim = img_nii.header.get_zooms()
    sform = img_nii.get_sform()
    qform = img_nii.get_qform()
    norm_sform = np.sqrt(np.sum(np.square(sform[0:3, 0:3]), 0))
    norm_qform = np.sqrt(np.sum(np.square(qform[0:3, 0:3]), 0))
    flag_sform_problem = False
    flag_qform_problem = False
    if not np.array_equal(norm_sform, np.asarray(pixdim)):
        flag_sform_problem = True
    if not np.array_equal(norm_qform, np.asarray(pixdim)):
        flag_qform_problem = True

    if img_nii.header['sform_code'] > 0:
        if not flag_sform_problem:
            return img_nii
        elif not flag_qform_problem:
            # recover by copying the qform over the sform
            img_nii.set_sform(np.copy(img_nii.get_qform()))
            return img_nii
    elif img_nii.header['qform_code'] > 0:
        if not flag_qform_problem:
            return img_nii
        elif not flag_sform_problem:
            # recover by copying the sform over the qform
            img_nii.set_qform(np.copy(img_nii.get_sform()))
            return img_nii
    affine = img_nii.affine
    pixdim = img_nii.header.get_zooms()[:3]  # TODO: assuming 3 elements
    new_affine = create_affine_pixdim(affine, pixdim)
    img_nii.set_sform(new_affine)
    img_nii.set_qform(new_affine)
    return img_nii


#### end of utilities for file headers


### resample/reorientation original volumes
# Perform the reorientation to ornt_fin of the data array given ornt_init
def do_reorientation(data_array, init_axcodes, final_axcodes):
    '''
    Performs the reorientation (changing order of axes)
    :param data_array: Array to reorient
    :param ornt_init: Initial orientation
    :param ornt_fin: Target orientation
    :return data_reoriented: New data array in its reoriented form
    '''
    ornt_init = nib.orientations.axcodes2ornt(init_axcodes)
    ornt_fin = nib.orientations.axcodes2ornt(final_axcodes)
    if np.array_equal(ornt_init, ornt_fin):
        return data_array
    ornt_transf = nib.orientations.ornt_transform(ornt_init, ornt_fin)
    data_reoriented = nib.orientations.apply_orientation(
        data_array, ornt_transf)
    return data_reoriented


# Perform the resampling of the data array given the initial and final pixel
# dimensions and the interpolation order
# this function assumes the same interp_order for multi-modal images
# do we need separate interp_order for each modality?
def do_resampling(data_array, pixdim_init, pixdim_fin, interp_order):
    '''
    Performs the resampling
    :param data_array: Data array to resample
    :param pixdim_init: Initial pixel dimension
    :param pixdim_fin: Targeted pixel dimension
    :param interp_order: Interpolation order applied
    :return data_resampled: Array containing the resampled data
    '''
    if data_array is None:
        # do nothing
        return
    if np.array_equal(pixdim_fin, pixdim_init):
        return data_array
    to_multiply = np.divide(pixdim_init, pixdim_fin[:len(pixdim_init)])
    data_shape = data_array.shape
    if len(data_shape) != 5:
        raise ValueError("only supports 5D array resampling, "
                         "input shape {}".format(data_shape))
    data_resampled = []
    for t in range(0, data_shape[3]):
        data_mod = []
        for m in range(0, data_shape[4]):
            data_new = scipy.ndimage.zoom(data_array[..., t, m],
                                          to_multiply[0:3],
                                          order=interp_order)
            data_mod.append(data_new[..., np.newaxis, np.newaxis])
        data_mod = np.concatenate(data_mod, axis=-1)
        data_resampled.append(data_mod)
    data_resampled = np.concatenate(data_resampled, axis=-2)
    return data_resampled


### end of resample/reorientation original volumes

def save_data_array(filefolder,
                    filename,
                    array_to_save,
                    image_object=None,
                    interp_order=3):
    """
    write image data array to hard drive using image_object
    properties such as affine, pixdim and axcodes.
    """
    if image_object is not None:
        affine = image_object.original_affine[0]
        image_pixdim = image_object.output_pixdim[0]
        image_axcodes = image_object.output_axcodes[0]
        dst_pixdim = image_object.original_pixdim[0]
        dst_axcodes = image_object.original_axcodes[0]
    else:
        affine = np.eye(4)
        image_pixdim, image_axcodes = (), ()

    if len(array_to_save.shape) == 4:
        # recover a time dimension for nifti format output
        array_to_save = np.expand_dims(array_to_save, axis=3)
    if image_pixdim:
        array_to_save = do_resampling(
            array_to_save, image_pixdim, dst_pixdim, interp_order)
    if image_axcodes:
        array_to_save = do_reorientation(
            array_to_save, image_axcodes, dst_axcodes)
    save_volume_5d(array_to_save, filename, filefolder, affine)


def expand_to_5d(img_data):
    '''
    Expands an array up to 5d if it is not the case yet
    :param img_data:
    :return:
    '''
    while img_data.ndim < 5:
        img_data = np.expand_dims(img_data, axis=-1)
    return img_data


def save_volume_5d(img_data, filename, save_path, affine=np.eye(4)):
    '''
    Save the img_data to nifti image
    :param img_data: 5d img to save
    :param filename: filename under which to save the img_data
    :param save_path:
    :param affine: an affine matrix.
    :return:
    '''
    if img_data is None:
        return
    try:
        if not os.path.exists(save_path):
            os.makedirs(save_path)
    except OSError:
        tf.logging.fatal('writing output images failed.')
        raise

    img_nii = nib.Nifti1Image(img_data, affine)
    # img_nii.set_data_dtype(np.dtype(np.float32))
    output_name = os.path.join(save_path, filename)
    try:
        nib.save(img_nii, output_name)
    except OSError:
        tf.logging.fatal("writing failed {}".format(output_name))
        raise
    print('Saved {}'.format(output_name))


def split_filename(file_name):
    pth = os.path.dirname(file_name)
    fname = os.path.basename(file_name)

    ext = None
    for special_ext in FILE_EXTENSIONS:
        ext_len = len(special_ext)
        if fname[-ext_len:].lower() == special_ext:
            ext = fname[-ext_len:]
            fname = fname[:-ext_len] if len(fname) > ext_len else ''
            break
    if not ext:
        fname, ext = os.path.splitext(fname)
    return pth, fname, ext


def squeeze_spatial_temporal_dim(tf_tensor):
    """
    Given a tensorflow tensor, ndims==6 means:
    [batch, x, y, z, time, modality]
    this function remove the time dim if it's one
    """
    if tf_tensor.get_shape().ndims != 6:
        return tf_tensor
    if tf_tensor.get_shape()[4] != 1:
        raise NotImplementedError("time sequences not currently supported")
    axis_to_squeeze = []
    for (idx, axis) in enumerate(tf_tensor.shape.as_list()):
        if idx == 0 or idx == 5:
            continue
        if axis == 1:
            axis_to_squeeze.append(idx)
    return tf.squeeze(tf_tensor, axis=axis_to_squeeze)