from typing import Literal

import numpy as np
import xarray as xr
from affine import Affine
from shapely import Polygon


def make_template(data: xr.DataArray) -> xr.DataArray:
    """
    Create a template DataArray with the same structure as `data` but filled with object data type.

    Args:
        data (xr.DataArray): The input data array for which the template will be created.

    Returns:
        xr.DataArray: A template data array filled with the object data type.

    Raises:
        ValueError: If the input data is not chunked.

    Example:
        >>> arr = xr.DataArray(np.random.rand(4, 3)).chunk((2, 2))
        >>> template = make_template(arr)
        >>> print(template)
        <xarray.DataArray (dim_0: 2, dim_1: 2)>
        dask.array<astype, shape=(2, 2), dtype=object, chunksize=(2, 2), chunktype=numpy.ndarray>
        Dimensions without coordinates: dim_0, dim_1
    """

    if data.chunks is None:
        msg = "The input data array must be chunked."
        raise ValueError(msg)

    offsets = dict(
        zip(
            data.dims,
            [np.hstack([np.array(0), np.cumsum(x)[:-1]]) for x in data.chunks],
            strict=True,
        )
    )
    template = data.isel(**offsets).astype(object)  # type: ignore

    return template


def extract_and_set_nodata(
    ds: xr.Dataset, vars_with_nodata: list[str], vars_without_nodata: list[str]
) -> tuple[xr.Dataset, float | int]:
    """
    Extracts unique nodata values from the dataset and sets it for the given data variables.

    Args:
        ds (xr.Dataset): Dataset to extract nodata from and set nodata for.
        vars_with_nodata (list[str]): List of data variables that already have nodata values set.
        vars_without_nodata (list[str]): List of data variables to which the common nodata value should be applied.

    Returns:
        tuple[xr.Dataset, float | int]:
            - ds: Dataset with nodata values set for the given data variables.
            - common_nodata_value: The common nodata value set for the data variables.

    Raises:
        ValueError: If no nodata values are found or multiple unique nodata values exist among the specified variables.

    Example:
        >>> ds = xr.Dataset({
        ...    "a": (["x", "y"], np.random.rand(4, 3)),
        ...    "b": (["x", "y"], np.random.rand(4, 3))
        ... })
        >>> ds['a'].rio.write_nodata(-999)
        >>> ds_updated, nodata_val = extract_and_set_nodata(ds, ["a"], ["b"])
        >>> print(nodata_val)
        -999
    """

    nodata_values = [ds[var].rio.nodata for var in vars_with_nodata]
    nodata_values = [val for val in nodata_values if val is not None]
    unique_nodata_values = np.unique(nodata_values)

    # Check if the nodata values are consistent
    if len(unique_nodata_values) == 0:
        msg = "No nodata value found for the specified variables."
        raise ValueError(msg)
    elif len(unique_nodata_values) > 1:
        msg = "Multiple nodata values found. Ensure consistent nodata values."
        raise ValueError(msg)

    common_nodata_value = unique_nodata_values[0]
    for var in vars_without_nodata:
        ds[var] = ds[var].rio.write_nodata(common_nodata_value)

    return ds, common_nodata_value


def raster_center(ds: xr.Dataset) -> tuple[float, float]:
    """
    Calculate the center of a raster dataset.

    Args:
        ds (xr.Dataset): The raster dataset.

    Returns:
        Tuple[float, float]: A tuple representing the (x, y) coordinates of the raster's center.

    Example:
        >>> ds = xr.Dataset(...)
        >>> raster_center(ds)
        (150.0, 100.0)
    """
    width, height = ds.rio.width, ds.rio.height
    x_middle = width / 2
    y_middle = height / 2
    return (x_middle, y_middle)


def rotate_raster(
    ds: xr.Dataset, rotation_angle: float, pivot: tuple[float, float] | None = None
) -> xr.Dataset:
    """
    Rotate a raster dataset around a pivot point or its center.

    Args:
        ds (xr.Dataset): The raster dataset to be rotated.
        rotation_angle (float): Angle to rotate the raster, in degrees. Positive values represent counterclockwise rotation.
        pivot (Tuple[float, float], optional): The (x, y) coordinates of the pivot point. If not provided, raster's center is used.

    Returns:
        xr.Dataset: The rotated raster dataset.

    Example:
        >>> ds = xr.Dataset(...)
        >>> rotated_ds = rotate_raster(ds, 45)
    """
    src_transform = ds.rio.transform()
    rotation = Affine.rotation(rotation_angle, pivot=pivot)
    dst_transform = src_transform * rotation

    # Rescale the y-axis to correct the inversion
    rescale_y = Affine(1, 0, 0, 0, -1, ds.rio.height)
    dst_transform = dst_transform * rescale_y

    ds = ds.rio.reproject(dst_crs=ds.rio.crs, transform=dst_transform)
    ds = ds.rio.write_transform(dst_transform)
    ds = ds.assign_coords(
        {"y": ("y", range(ds.dims["y"])), "x": ("x", range(ds.dims["x"]))}
    )
    return ds


def interpolate_raster(
    ds: xr.Dataset,
    y_shape: int,
    x_shape: int,
    method: Literal[
        "linear",
        "nearest",
        "zero",
        "slinear",
        "quadratic",
        "cubic",
        "polynomial",
        "barycentric",
        "krog",
        "pchip",
        "spline",
        "akima",
    ],
) -> xr.Dataset:
    """
    Interpolates a given raster (xarray Dataset) to a specified resolution using the provided method.

    Args:
        ds (xr.Dataset): The input raster to interpolate.
        y_shape (int): Desired number of grid points along y dimension.
        x_shape (int): Desired number of grid points along x dimension.
        method (str, optional): The interpolation method to use. Defaults to "linear".
                                Other methods like "nearest", "cubic" can also be used.

    Returns:
        xr.Dataset: Interpolated raster without geospatial metadata.

    Example:
        >>> ds = xr.Dataset(data_vars={"var": (("x", "y"), np.random.randn(10, 10))},
        ...                 coords={"x": np.linspace(0, 9, 10), "y": np.linspace(0, 9, 10)})
        >>> interpolated_ds = interpolate_raster(ds, 20, 20)
        >>> print(interpolated_ds.dims)
        {'x': 20, 'y': 20}
    """

    # swap dims if y is longer than x
    if ds.dims["x"] < ds.dims["y"]:
        y_shape, x_shape = x_shape, y_shape

    # this will be the new grid
    new_y = np.linspace(ds.y.min(), ds.y.max(), y_shape)
    new_x = np.linspace(ds.x.min(), ds.x.max(), x_shape)

    interpolated = ds.interp(y=new_y, x=new_x, method=method)

    # add new coords because the old ones are now two dimensional
    interpolated = interpolated.assign_coords(y=new_y, x=new_x)

    # the transformation matrix can be computed by scaling the src one
    src_dims = ds.dims
    src_transform = ds.rio.transform()
    x_scale = src_dims["x"] / x_shape
    y_scale = src_dims["y"] / y_shape

    # write new transformation matrix to the interplated dataset
    dst_transform = src_transform * Affine.scale(x_scale, y_scale)
    interpolated = interpolated.rio.write_transform(dst_transform)

    return interpolated


def trim_outer_nans(
    data: xr.DataArray | xr.Dataset,
    crop_size: int = 1,
    nodata: float | int | None = None,
) -> xr.DataArray | xr.Dataset:
    """
    Trim the outer nodata or NaN values from an xarray DataArray or Dataset, returning a bounding box around the data.

    Args:
        data (xr.DataArray | xr.Dataset): Input DataArray or Dataset with potential outer NaN or nodata values.
        crop_size (int): The number of pixels to crop from the outer edges of the data. Defaults to 1.
        nodata (float | int | None): Optional no-data value to use for trimming. Defaults to None, which uses NaN.

    Returns:
        (xr.DataArray | xr.Dataset): A DataArray or Dataset trimmed of its outer NaN or nodata values.
    """

    # Determine the representative DataArray for NaN or nodata calculation
    ref_data_array = (
        data[next(iter(data.data_vars.keys()))]
        if isinstance(data, xr.Dataset)
        else data
    )
    transform = ref_data_array.rio.transform()

    mask = ref_data_array != nodata if nodata is not None else ~np.isnan(ref_data_array)
    y_valid, x_valid = np.where(mask)

    # If no valid data exists, return the input as-is
    if not y_valid.size or not x_valid.size:
        return data

    # Compute bounding indices (adjusting by the crop_size in pixels as before)
    y_min, y_max = y_valid.min() + crop_size, y_valid.max() - crop_size
    x_min, x_max = x_valid.min() + crop_size, x_valid.max() - crop_size

    # In Python slicing is end exclusive, so we need to add 1 to the max indices
    trimmed_data = data.isel(y=slice(y_min, y_max + 1), x=slice(x_min, x_max + 1))

    # Adjust the x,y offsets, also taking into account the rotation
    new_c = transform.c + x_min * transform.a + y_min * transform.b
    new_f = transform.f + x_min * transform.d + y_min * transform.e

    # Make the new transformation matrix and write to the array
    new_transform = Affine(
        transform.a,
        transform.b,
        new_c,
        transform.d,
        transform.e,
        new_f,
    )
    trimmed_data = trimmed_data.rio.write_transform(new_transform)

    return trimmed_data


def extract_data_inside_polygon(rotated: xr.Dataset, polygon: Polygon) -> xr.Dataset:
    """
    Extracts the data inside the bounding box of a given polygon from a rotated xarray dataset.

    Args:
        rotated (xr.Dataset): The rotated dataset to extract data from.
        polygon (Polygon): The polygon defining the region of interest.

    Returns:
        xr.Dataset: The subset of the rotated dataset inside the polygon's bounding box.

    Example:
        >>> rotated_ds = <Your rotated xarray dataset>
        >>> polygon = Polygon([(x1, y1), (x2, y2), ...])
        >>> subset = extract_data_inside_polygon(rotated_ds, polygon)
        >>> print(subset)
    """

    x_coords, y_coords = zip(*polygon.exterior.coords, strict=True)
    x_indices, y_indices = [], []

    for x_val, y_val in zip(x_coords, y_coords, strict=True):
        # Compute distance to every grid point for x and y coordinates separately
        distance_x = abs(rotated.xc - x_val)
        distance_y = abs(rotated.yc - y_val)

        # Use np.unravel_index to get the indices of the minimum distance
        y_idx, x_idx = np.unravel_index(
            (distance_x + distance_y).argmin().values, distance_x.shape
        )

        x_indices.append(x_idx)
        y_indices.append(y_idx)

    # Determine the slice bounds
    x_slice = slice(min(x_indices), max(x_indices) + 1)
    y_slice = slice(min(y_indices), max(y_indices) + 1)

    # Slice the dataset
    subset = rotated.isel(x=x_slice, y=y_slice)

    return subset
