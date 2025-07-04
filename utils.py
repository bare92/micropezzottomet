#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Mon Jun 30 14:20:57 2025

@author: rbarella
"""

import json
import rasterio
import xarray as xr
import os
import numpy as np
import pandas as pd


def parse_yes_no_flag(value, var_name=""):
    """
    Converts 'y'/'n' string flags to boolean.
    
    Parameters:
        value (str): The input string, expected to be 'y' or 'n'.
        var_name (str): Optional variable name for clearer error messages.
        
    Returns:
        bool: True if 'y', False if 'n'.
        
    Raises:
        ValueError: If value is not 'y' or 'n'.
    """
    if value == "y":
        return True
    elif value == "n":
        return False
    else:
        raise ValueError(f"Invalid value for '{var_name}': {value}. Expected 'y' or 'n'.")


def create_full_micromet_folder_structure(base_path="."):
    folders = [
        "inputs/climate",
        "inputs/dem",
        "outputs"
    ]

    for folder in folders:
        path = os.path.join(base_path, folder)
        os.makedirs(path, exist_ok=True)

    print("Micromet folder structure created successfully.")


def load_config(config_path):
    with open(config_path, 'r') as f:
        config = json.load(f)
    return config

def load_dem(dem_path):
    with rasterio.open(dem_path) as src:
        dem_data = src.read(1)
        dem_meta = src.meta
        dem_transform = src.transform
    return dem_data, dem_meta, dem_transform

def load_era_data(era_path, variables, start_date=None, end_date=None):
    era_ds = xr.open_dataset(era_path)

    # Optionally select variables and time range
    era_ds = era_ds[variables]
    if start_date and end_date and "time" in era_ds.dims:
        era_ds = era_ds.sel(time=slice(start_date, end_date))

    return era_ds


def compute_slope_aspect(dem_path, working_directory):
    """
    Compute slope and aspect from a DEM using gdaldem and save results to <working_directory>/input/dem.

    Parameters:
        dem_path (str): Path to the input DEM file.
        working_directory (str): Path to the working directory.
    
    Returns:
        slope_path (str), aspect_path (str): Paths to the generated output files.
    """
    output_dir = os.path.join(working_directory, 'inputs', 'dem')
    os.makedirs(output_dir, exist_ok=True)

    # Define output file paths
    slope_path = os.path.join(output_dir, 'slope.tif')
    aspect_path = os.path.join(output_dir, 'aspect.tif')

    # Build and run gdaldem commands
    slope_cmd = f'gdaldem slope "{dem_path}" "{slope_path}" -of GTiff'
    aspect_cmd = f'gdaldem aspect "{dem_path}" "{aspect_path}" -of GTiff'

    slope_status = os.system(slope_cmd)
    aspect_status = os.system(aspect_cmd)

    if slope_status == 0 and aspect_status == 0:
        print(f"Slope and aspect successfully saved in {output_dir}")
    else:
        print("Error running gdaldem commands")

    return slope_path, aspect_path



def compute_topographic_curvature(dem_path, working_directory, L=1000, dem_nodata=None):
    """
    Compute and save curvature from DEM using vectorized numpy operations (fast version),
    masking out no-data values.

    Parameters:
        dem_path (str): Path to input DEM file
        working_directory (str): Output folder
        L (float): Curvature length scale (m)
        dem_nodata (float or int): No-data value in DEM

    Returns:
        curvature_path (str): Path to saved curvature GeoTIFF
    """
    output_dir = os.path.join(working_directory, 'inputs', 'dem')
    os.makedirs(output_dir, exist_ok=True)
    curvature_path = os.path.join(output_dir, 'curvature.tif')

    if os.path.exists(curvature_path):
        print(f"Curvature already exists at {curvature_path}. Skipping.")
        return curvature_path

    with rasterio.open(dem_path) as src:
        dem = src.read(1).astype(np.float32)
        transform = src.transform
        dem_meta = src.meta.copy()

    if dem_nodata is not None:
        dem[dem == dem_nodata] = np.nan

    ny, nx = dem.shape
    deltax = transform.a
    deltay = -transform.e
    deltaxy = 0.5 * (deltax + deltay)
    inc = max(1, int(round(L / deltaxy)))

    dem_pad = np.pad(dem, inc, mode='edge')

    # Prepare shifted arrays
    z = dem_pad[inc:-inc, inc:-inc]
    zW = dem_pad[inc:-inc, inc - inc:-2 * inc]
    zE = dem_pad[inc:-inc, inc + inc:2 * inc + inc]
    zS = dem_pad[inc + inc:2 * inc + inc, inc:-inc]
    zN = dem_pad[inc - inc:-2 * inc, inc:-inc]
    zSW = dem_pad[inc + inc:2 * inc + inc, inc - inc:-2 * inc]
    zNE = dem_pad[inc - inc:-2 * inc, inc + inc:2 * inc + inc]
    zNW = dem_pad[inc - inc:-2 * inc, inc - inc:-2 * inc]
    zSE = dem_pad[inc + inc:2 * inc + inc, inc + inc:2 * inc + inc]

    # Align shapes
    common_shape = np.min([arr.shape for arr in [z, zW, zE, zS, zN, zSW, zNE, zNW, zSE]], axis=0)
    def crop(arr): return arr[:common_shape[0], :common_shape[1]]
    z, zW, zE, zS, zN = map(crop, [z, zW, zE, zS, zN])
    zSW, zNE, zNW, zSE = map(crop, [zSW, zNE, zNW, zSE])

    # Compute curvature
    c_diag = (4 * z - zSW - zNE - zNW - zSE) / (np.sqrt(2.0) * 16.0 * inc * deltaxy)
    c_cross = (4 * z - zW - zE - zN - zS) / (16.0 * inc * deltaxy)
    curvature = c_diag + c_cross

    curvature[np.isnan(z)] = np.nan

    curve_max = max(0.001, np.nanmax(np.abs(curvature)))
    curvature /= (2.0 * curve_max)

    # Embed curvature into full DEM shape
    full_curv = np.full_like(dem, np.nan, dtype=np.float32)
    valid_shape = curvature.shape
    full_curv[inc:inc + valid_shape[0], inc:inc + valid_shape[1]] = curvature

    dem_meta.update(dtype='float32', count=1)
    with rasterio.open(curvature_path, 'w', **dem_meta) as dst:
        dst.write(full_curv, 1)

    print(f"Curvature saved to {curvature_path}")
    return curvature_path


def write_downscaled_to_netcdf(
    variables_dict,
    time_list,
    dem_shape,
    dem_transform,
    dem_crs,
    out_nc
):
    """
    Save multiple downscaled variables to NetCDF with spatial referencing.

    Parameters:
        variables_dict: dict of {var_name: (data_list, units, description)}
        time_list: list of datetime objects
        dem_shape: shape of the DEM used as reference
        dem_transform: Affine transform of the DEM
        dem_crs: CRS of the DEM
        out_nc: full path to the output NetCDF file
    """

    height, width = dem_shape
    x_coords = np.arange(width) * dem_transform.a + dem_transform.c + dem_transform.a / 2
    y_coords = np.arange(height) * dem_transform.e + dem_transform.f + dem_transform.e / 2

    dataset_vars = {}

    for var_name, (data_list, units, description) in variables_dict.items():
        data_stack = np.concatenate(data_list, axis=0)

        da = xr.DataArray(
            data_stack,
            dims=["time", "y", "x"],
            coords={"time": time_list, "y": y_coords, "x": x_coords},
            attrs={"units": units, "description": description}
        )

        dataset_vars[var_name] = da

    ds_out = xr.Dataset(dataset_vars)
    ds_out = ds_out.rio.write_transform(dem_transform)
    ds_out = ds_out.rio.write_crs(dem_crs)

    os.makedirs(os.path.dirname(out_nc), exist_ok=True)
    ds_out.to_netcdf(out_nc)

    print(f"\nSaved NetCDF: {out_nc}")





