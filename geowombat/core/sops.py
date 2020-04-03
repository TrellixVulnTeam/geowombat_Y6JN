import os
import math
import itertools
from datetime import datetime

from ..errors import logger
from ..backends.rasterio_ import align_bounds, array_bounds, aligned_target
from .conversion import Converters
from .base import PropertyMixin as _PropertyMixin
from .util import lazy_wombat

import numpy as np
from scipy.spatial import cKDTree
import pandas as pd
import geopandas as gpd
import xarray as xr
import dask
import dask.array as da
from rasterio.crs import CRS
from rasterio import features
from affine import Affine

try:
    import arosics
    AROSICS_INSTALLED = True
except:
    AROSICS_INSTALLED = False

try:
    import pymorph
    PYMORPH_INSTALLED = True
except:
    PYMORPH_INSTALLED = False


def _remove_near_points(dataframe, r):

    """
    Removes points less than a specified distance to another point

    Args:
        dataframe (GeoDataFrame): The ``GeoDataFrame`` with point geometry.
        r (float or int): The minimum distance (radius) in the CRS units of ``dataframe``.

    Returns:
        ``geopandas.GeoDataFrame``
    """

    # Setup a KD tree
    tree = cKDTree(np.c_[dataframe.geometry.x, dataframe.geometry.y])

    # Query all pairs within ``min_dist`` of each other
    near_pairs = tree.query_pairs(r=r, output_type='ndarray')

    if near_pairs.shape[0] > 0:

        # Get a list of pairs to remove
        rm_idx = list(sorted(set(near_pairs[:, 0].tolist())))

        return dataframe.query("index != {}".format(rm_idx))

    return dataframe


def _transform_and_shift(affine_transform, col_indices, row_indices, cellxh, cellyh):

    """
    Transforms indices to coordinates and applies a half pixel shift

    Args:
        affine_transform (object): The affine transform.
        col_indices (1d array): The column indices.
        row_indices (1d array): The row indices.
        cellxh (float): The half cell width in the x direction.
        cellyh (float): The half cell width in the y direction.

    Returns:
        ``numpy.ndarray``, ``numpy.ndarray``
    """

    x_coords, y_coords = affine_transform * (col_indices, row_indices)

    x_coords += abs(cellxh)
    y_coords -= abs(cellyh)

    return x_coords, y_coords


class SpatialOperations(_PropertyMixin):

    def sample(self,
               data,
               method='random',
               band=None,
               n=None,
               strata=None,
               spacing=None,
               min_dist=None,
               max_attempts=10,
               **kwargs):

        """
        Generates samples from a raster

        Args:
            data (DataArray): The ``xarray.DataArray`` to extract data from.
            method (Optional[str]): The sampling method. Choices are ['random', 'systematic'].
            band (Optional[int or str]): The band name to extract from. Only required if ``method`` = 'random' and ``strata`` is given.
            n (Optional[int]): The total number of samples. Only required if ``method`` = 'random'.
            strata (Optional[dict]): The strata to sample within. The dictionary key-->value pairs should be {'conditional,value': sample size}.

                E.g.,

                    strata = {'==,1': 0.5, '>=,2': 0.5}
                    ... would sample 50% of total samples within class 1 and 50% of total samples in class >= 2.

                    strata = {'==,1': 10, '>=,2': 20}
                    ... would sample 10 samples within class 1 and 20 samples in class >= 2.

            spacing (Optional[float]): The spacing (in map projection units) when ``method`` = 'systematic'.
            min_dist (Optional[float or int]): A minimum distance allowed between samples. Only applies when ``method`` = 'random'.
            max_attempts (Optional[int]): The maximum numer of attempts to sample points > ``min_dist`` from each other.
            kwargs (Optional[dict]): Keyword arguments passed to ``geowombat.extract``.

        Returns:
            ``geopandas.GeoDataFrame``

        Examples:
            >>> import geowombat as gw
            >>>
            >>> # Sample 100 points randomly across the image
            >>> with gw.open('image.tif') as src:
            >>>     df = gw.sample(src, n=100)
            >>>
            >>> # Sample points systematically (with 10km spacing) across the image
            >>> with gw.open('image.tif') as src:
            >>>     df = gw.sample(src, method='systematic', spacing=10000.0)
            >>>
            >>> # Sample 50% of 100 in class 1 and 50% in classes >= 2
            >>> strata = {'==,1': 0.5, '>=,2': 0.5}
            >>> with gw.open('image.tif') as src:
            >>>     df = gw.sample(src, band=1, n=100, strata=strata)
            >>>
            >>> # Specify a per-stratum minimum allowed point distance of 1,000 meters
            >>> with gw.open('image.tif') as src:
            >>>     df = gw.sample(src, band=1, n=100, min_dist=1000, strata=strata)
        """

        if method.strip().lower() not in ['random', 'systematic']:
            raise NameError("The method must be 'random' or 'systematic'.")

        if method.strip().lower() == 'systematic':
            if not isinstance(spacing, float):
                if not isinstance(spacing, int):

                    logger.exception("  If the method is 'systematic', the spacing should be provided as a float or integer.")
                    raise TypeError

        if strata and not band and (method.strip().lower() == 'random'):

            logger.exception('  The band name must be provided with random stratified sampling.')
            raise NameError

        df = None

        if not strata:

            if method == 'systematic':

                x_samples = list()
                y_samples = list()

                for i in range(0, data.gw.nrows, int(spacing / data.gw.celly)):
                    for j in range(0, data.gw.ncols, int(spacing / data.gw.cellx)):

                        x_samples.append(j)
                        y_samples.append(i)

                x_samples = np.array(x_samples, dtype='int64')
                y_samples = np.array(y_samples, dtype='int64')

                # Convert the map indices to map coordinates
                x_coords, y_coords = _transform_and_shift(data.gw.meta.affine,
                                                          x_samples,
                                                          y_samples,
                                                          data.gw.cellxh,
                                                          data.gw.cellyh)

                df = gpd.GeoDataFrame(data=range(0, x_coords.shape[0]),
                                      geometry=gpd.points_from_xy(x_coords, y_coords),
                                      crs=data.crs,
                                      columns=['point'])

            else:

                dfs = None
                sample_size = n
                attempts = 0

                while True:

                    if attempts >= max_attempts:

                        logger.warning('  Max attempts reached. Try relaxing the distance threshold.')
                        break

                        # Sample directly from the coordinates
                    y_coords = np.random.choice(data.y.values, size=sample_size, replace=False)
                    x_coords = np.random.choice(data.x.values, size=sample_size, replace=False)

                    if isinstance(dfs, gpd.GeoDataFrame):

                        dfs = pd.concat((dfs, gpd.GeoDataFrame(data=range(0, x_coords.shape[0]),
                                                               geometry=gpd.points_from_xy(x_coords, y_coords),
                                                               crs=data.crs,
                                                               columns=['point'])), axis=0)

                    else:

                        dfs = gpd.GeoDataFrame(data=range(0, x_coords.shape[0]),
                                               geometry=gpd.points_from_xy(x_coords, y_coords),
                                               crs=data.crs,
                                               columns=['point'])

                    if isinstance(min_dist, float) or isinstance(min_dist, int):

                        # Remove samples within a minimum distance
                        dfn = _remove_near_points(dfs, min_dist)

                        df_diff = dfs.shape[0] - dfn.shape[0]

                        if df_diff > 0:
                            dfs = dfn.copy()
                            sample_size = df_diff

                            attempts += 1

                            continue

                    break

                df = dfs.copy()

        else:

            counter = 0
            dfs = None

            for cond, stratum_size in strata.items():

                sign, value = cond.split(',')
                sign = sign.strip()
                value = float(value)

                if isinstance(stratum_size, int):
                    sample_size = stratum_size
                else:
                    sample_size = int(n * stratum_size)

                attempts = 0

                while True:

                    if attempts >= 50:

                        logger.warning('  Max attempts reached for value {:f}. Try relaxing the distance threshold.'.format(value))

                        if not isinstance(df, gpd.GeoDataFrame):
                            df = dfs.copy()
                        else:
                            df = pd.concat((df, dfs), axis=0)

                        break

                    if sign == '>':
                        valid_samples = da.where(data.sel(band=band).data > value)
                    elif sign == '>=':
                        valid_samples = da.where(data.sel(band=band).data >= value)
                    elif sign == '<':
                        valid_samples = da.where(data.sel(band=band).data < value)
                    elif sign == '<=':
                        valid_samples = da.where(data.sel(band=band).data <= value)
                    elif sign == '==':
                        valid_samples = da.where(data.sel(band=band).data == value)
                    else:
                        logger.exception("  The conditional sign was not recognized. Use one of '>', '>=', '<', '<=', or '=='.")
                        raise NameError

                    valid_samples = dask.compute(valid_samples)[0]

                    y_samples = valid_samples[0]
                    x_samples = valid_samples[1]

                    if y_samples.shape[0] > 0:

                        # Get indices within the stratum
                        idx = np.random.choice(range(0, y_samples.shape[0]), size=sample_size, replace=False)

                        y_samples = y_samples[idx]
                        x_samples = x_samples[idx]

                        # Convert the map indices to map coordinates
                        x_coords, y_coords = _transform_and_shift(data.gw.meta.affine,
                                                                  x_samples,
                                                                  y_samples,
                                                                  data.gw.cellxh,
                                                                  data.gw.cellyh)

                        if isinstance(dfs, gpd.GeoDataFrame):

                            dfs = pd.concat((dfs, gpd.GeoDataFrame(data=range(0, x_coords.shape[0]),
                                                                   geometry=gpd.points_from_xy(x_coords, y_coords),
                                                                   crs=data.crs,
                                                                   columns=['point'])), axis=0)

                        else:

                            dfs = gpd.GeoDataFrame(data=range(0, x_coords.shape[0]),
                                                   geometry=gpd.points_from_xy(x_coords, y_coords),
                                                   crs=data.crs,
                                                   columns=['point'])

                        if isinstance(min_dist, float) or isinstance(min_dist, int):

                            # Remove samples within a minimum distance
                            dfn = _remove_near_points(dfs, min_dist)

                            df_diff = dfs.shape[0] - dfn.shape[0]

                            if df_diff > 0:
                                dfs = dfn.copy()
                                sample_size = df_diff

                                attempts += 1

                                continue

                        if not isinstance(df, gpd.GeoDataFrame):
                            df = dfs.copy()
                        else:
                            df = pd.concat((df, dfs), axis=0)

                        dfs = None

                    break

                counter += 1

        if isinstance(df, gpd.GeoDataFrame):
            return self.extract(data, df, **kwargs)
        else:
            return None

    def extract(self,
                data,
                aoi,
                bands=None,
                time_names=None,
                band_names=None,
                frac=1.0,
                all_touched=False,
                id_column='id',
                mask=None,
                n_jobs=8,
                verbose=0,
                **kwargs):

        """
        Extracts data within an area or points of interest. Projections do not need to match,
        as they are handled 'on-the-fly'.

        Args:
            data (DataArray): The ``xarray.DataArray`` to extract data from.
            aoi (str or GeoDataFrame): A file or ``geopandas.GeoDataFrame`` to extract data frame.
            bands (Optional[int or 1d array-like]): A band or list of bands to extract.
                If not given, all bands are used. Bands should be GDAL-indexed (i.e., the first band is 1, not 0).
            band_names (Optional[list]): A list of band names. Length should be the same as `bands`.
            time_names (Optional[list]): A list of time names.
            frac (Optional[float]): A fractional subset of points to extract in each polygon feature.
            all_touched (Optional[bool]): The ``all_touched`` argument is passed to ``rasterio.features.rasterize``.
            id_column (Optional[str]): The id column name.
            mask (Optional[GeoDataFrame or Shapely Polygon]): A ``shapely.geometry.Polygon`` mask to subset to.
            n_jobs (Optional[int]): The number of features to rasterize in parallel.
            verbose (Optional[int]): The verbosity level.
            kwargs (Optional[dict]): Keyword arguments passed to ``dask.compute``.

        Returns:
            ``geopandas.GeoDataFrame``

        Examples:
            >>> import geowombat as gw
            >>>
            >>> with gw.open('image.tif') as ds:
            >>>     df = gw.extract(ds, 'poly.gpkg')
        """

        sensor = self.check_sensor(data, return_error=False)

        band_names = self.check_sensor_band_names(data, sensor, band_names)

        converters = Converters()

        shape_len = data.gw.ndims

        if isinstance(bands, list):
            bands_idx = (np.array(bands, dtype='int64') - 1).tolist()
        elif isinstance(bands, np.ndarray):
            bands_idx = (bands - 1).tolist()
        elif isinstance(bands, int):
            bands_idx = [bands]
        else:

            if shape_len > 2:
                bands_idx = slice(0, None)
                
        if not id_column in data.columns:
            data['id_column'] = data.index.values

        df = converters.prepare_points(data,
                                       aoi,
                                       frac=frac,
                                       all_touched=all_touched,
                                       id_column=id_column,
                                       mask=mask,
                                       n_jobs=n_jobs,
                                       verbose=verbose)

        if verbose > 0:
            logger.info('  Extracting data ...')

        # Convert the map coordinates to indices
        x, y = converters.coords_to_indices(df.geometry.x.values,
                                            df.geometry.y.values,
                                            data.transform)

        vidx = (y.tolist(), x.tolist())

        if shape_len > 2:

            vidx = (bands_idx,) + vidx

            if shape_len > 3:

                # The first 3 dimensions are (bands, rows, columns)
                # TODO: allow user-defined time slice?
                for b in range(0, shape_len - 3):
                    vidx = (slice(0, None),) + vidx

        # Get the raster values for each point
        # TODO: allow neighbor indexing
        res = data.data.vindex[vidx].compute(**kwargs)

        if len(res.shape) == 1:
            df[band_names[0]] = res.flatten()
        elif len(res.shape) == 2:

            # `res` is shaped [samples x dimensions]
            df = pd.concat((df, pd.DataFrame(data=res, columns=band_names)), axis=1)

        else:

            # `res` is shaped [samples x time x dimensions]
            if time_names:

                if isinstance(time_names[0], datetime):
                    time_names = list(itertools.chain(*[[t.strftime('%Y-%m-%d')]*res.shape[2] for t in time_names]))
                else:
                    time_names = list(itertools.chain(*[[t]*res.shape[2] for t in time_names]))

            else:
                time_names = list(itertools.chain(*[['t{:d}'.format(t)]*res.shape[2] for t in range(1, res.shape[1]+1)]))

            band_names_concat = ['{}_{}'.format(a, b) for a, b in list(zip(time_names, band_names*res.shape[1]))]

            df = pd.concat((df,
                            pd.DataFrame(data=res.reshape(res.shape[0],
                                                          res.shape[1]*res.shape[2]),
                                         columns=band_names_concat)),
                           axis=1)

        return df

    def clip(self,
             data,
             df,
             query=None,
             mask_data=False,
             expand_by=0):

        """
        Clips a DataArray by vector polygon geometry

        Args:
            data (DataArray): The ``xarray.DataArray`` to subset.
            df (GeoDataFrame or str): The ``geopandas.GeoDataFrame`` or filename to clip to.
            query (Optional[str]): A query to apply to ``df``.
            mask_data (Optional[bool]): Whether to mask values outside of the ``df`` geometry envelope.
            expand_by (Optional[int]): Expand the clip array bounds by ``expand_by`` pixels on each side.

        Returns:
             ``xarray.DataArray``

        Examples:
            >>> import geowombat as gw
            >>>
            >>> with gw.open('image.tif') as ds:
            >>>     ds = gw.clip(ds, df, query="Id == 1")
            >>>
            >>> # or
            >>>
            >>> with gw.open('image.tif') as ds:
            >>>     ds = ds.gw.clip(df, query="Id == 1")
        """

        if isinstance(df, str) and os.path.isfile(df):
            df = gpd.read_file(df)

        if query:
            df = df.query(query)

        df_crs_ = df.crs.to_proj4().strip() if hasattr(df.crs, 'to_proj4') else df.crs

        # Re-project the DataFrame to match the image CRS
        try:

            if data.crs.strip() != CRS.from_dict(df_crs_).to_proj4().strip():
                df = df.to_crs(data.crs)

        except:

            if data.crs.strip() != CRS.from_proj4(df_crs_).to_proj4().strip():
                df = df.to_crs(data.crs)

        row_chunks = data.gw.row_chunks
        col_chunks = data.gw.col_chunks

        left, bottom, right, top = df.total_bounds

        # Align the geometry array grid
        align_transform, align_width, align_height = align_bounds(left,
                                                                  bottom,
                                                                  right,
                                                                  top,
                                                                  data.res)

        # Get the new bounds
        new_left, new_bottom, new_right, new_top = array_bounds(align_height,
                                                                align_width,
                                                                align_transform)

        if expand_by > 0:

            new_left -= data.gw.cellx*expand_by
            new_bottom -= data.gw.celly*expand_by
            new_right += data.gw.cellx*expand_by
            new_top += data.gw.celly*expand_by

        # Subset the array
        data = self.subset(data,
                           left=new_left,
                           bottom=new_bottom,
                           right=new_right,
                           top=new_top)

        if mask_data:

            # Rasterize the geometry and store as a DataArray
            mask = xr.DataArray(data=da.from_array(features.rasterize(list(df.geometry.values),
                                                                      out_shape=(align_height, align_width),
                                                                      transform=align_transform,
                                                                      fill=0,
                                                                      out=None,
                                                                      all_touched=True,
                                                                      default_value=1,
                                                                      dtype='int32'),
                                                   chunks=(row_chunks, col_chunks)),
                                dims=['y', 'x'],
                                coords={'y': data.y.values,
                                        'x': data.x.values})

            # Return the clipped array
            return data.where(mask == 1)

        else:
            return data

    @staticmethod
    @lazy_wombat
    def mask(data,
             dataframe,
             query=None,
             keep='in'):

        """
        Masks a DataArray by vector polygon geometry

        Args:
            data (DataArray): The ``xarray.DataArray`` to mask.
            dataframe (GeoDataFrame or str): The ``geopandas.GeoDataFrame`` or filename to use for masking.
            query (Optional[str]): A query to apply to ``dataframe``.
            keep (Optional[str]): If ``keep`` = 'in', mask values outside of the geometry (keep inside).
                Otherwise, if ``keep`` = 'out', mask values inside (keep outside).

        Returns:
             ``xarray.DataArray``

        Examples:
            >>> import geowombat as gw
            >>>
            >>> with gw.open('image.tif') as ds:
            >>>     ds = ds.gw.mask(df)
        """

        if isinstance(dataframe, str) and os.path.isfile(dataframe):
            dataframe = gpd.read_file(dataframe)

        if query:
            dataframe = dataframe.query(query)

        df_crs_ = dataframe.crs.to_proj4().strip() if hasattr(dataframe.crs, 'to_proj4') else dataframe.crs

        try:

            if data.crs.strip() != CRS.from_dict(df_crs_).to_proj4().strip():

                # Re-project the DataFrame to match the image CRS
                dataframe = dataframe.to_crs(data.crs)

        except:

            if data.crs.strip() != CRS.from_proj4(df_crs_).to_proj4().strip():
                dataframe = dataframe.to_crs(data.crs)

        # Rasterize the geometry and store as a DataArray
        mask = xr.DataArray(data=da.from_array(features.rasterize(list(dataframe.geometry.values),
                                                                  out_shape=(data.gw.nrows, data.gw.ncols),
                                                                  transform=data.transform,
                                                                  fill=0,
                                                                  out=None,
                                                                  all_touched=True,
                                                                  default_value=1,
                                                                  dtype='int32'),
                                               chunks=(data.gw.row_chunks, data.gw.col_chunks)),
                            dims=['y', 'x'],
                            coords={'y': data.y.values,
                                    'x': data.x.values})

        # Return the masked array
        if keep == 'out':
            return data.where(mask != 1)
        else:
            return data.where(mask == 1)

    @staticmethod
    def subset(data,
               left=None,
               top=None,
               right=None,
               bottom=None,
               rows=None,
               cols=None,
               center=False,
               mask_corners=False):

        """
        Subsets a DataArray

        Args:
            data (DataArray): The ``xarray.DataArray`` to subset.
            left (Optional[float]): The left coordinate.
            top (Optional[float]): The top coordinate.
            right (Optional[float]): The right coordinate.
            bottom (Optional[float]): The bottom coordinate.
            rows (Optional[int]): The number of output rows.
            cols (Optional[int]): The number of output rows.
            center (Optional[bool]): Whether to center the subset on ``left`` and ``top``.
            mask_corners (Optional[bool]): Whether to mask corners (*requires ``pymorph``).

        Returns:
            ``xarray.DataArray``

        Example:
            >>> import geowombat as gw
            >>>
            >>> with gw.open('image.tif', chunks=512) as ds:
            >>>
            >>>     ds_sub = gw.subset(ds,
            >>>                        left=-263529.884,
            >>>                        top=953985.314,
            >>>                        rows=2048,
            >>>                        cols=2048)
        """

        if isinstance(right, int) or isinstance(right, float):
            cols = int((right - left) / data.gw.celly)

        if not isinstance(cols, int):

            logger.exception('  The right coordinate or columns must be specified.')
            raise NameError

        if isinstance(bottom, int) or isinstance(bottom, float):
            rows = int((top - bottom) / data.gw.celly)

        if not isinstance(rows, int):

            logger.exception('  The bottom coordinate or rows must be specified.')
            raise NameError

        x_idx = np.linspace(math.ceil(left), math.ceil(left) + (cols * abs(data.gw.cellx)), cols) + abs(data.gw.cellxh)
        y_idx = np.linspace(math.ceil(top), math.ceil(top) - (rows * abs(data.gw.celly)), rows) - abs(data.gw.cellyh)

        if center:

            y_idx += ((rows / 2.0) * abs(data.gw.celly))
            x_idx -= ((cols / 2.0) * abs(data.gw.cellx))

        ds_sub = data.sel(y=y_idx,
                          x=x_idx,
                          method='nearest')

        if mask_corners:

            if PYMORPH_INSTALLED:

                try:

                    disk = da.from_array(pymorph.sedisk(r=int(rows/2.0))[:rows, :cols],
                                         chunks=ds_sub.data.chunksize).astype('uint8')
                    ds_sub = ds_sub.where(disk == 1)

                except:
                    logger.warning('  Cannot mask corners without a square subset.')

            else:
                logger.warning('  Cannot mask corners without Pymorph.')

        # Update the left and top coordinates
        transform = list(data.transform)

        transform[2] = x_idx[0]
        transform[5] = y_idx[0]

        # Align the coordinates to the target grid
        dst_transform, dst_width, dst_height = aligned_target(Affine(*transform),
                                                              ds_sub.shape[1],
                                                              ds_sub.shape[0],
                                                              data.res)

        ds_sub.attrs['transform'] = dst_transform

        return ds_sub

    @staticmethod
    def coregister(target,
                   reference,
                   **kwargs):

        """
        Co-registers an image, or images, using AROSICS.

        While the required inputs are DataArrays, the intermediate results are stored as NumPy arrays.
        Therefore, memory usage is constrained to the size of the input data. Dask is not used for any of the
        computation in this function.

        Args:
            target (DataArray or str): The target ``xarray.DataArray`` or file name to co-register to ``reference``.
            reference (DataArray or str): The reference ``xarray.DataArray`` or file name used to co-register ``target``.
            kwargs (Optional[dict]): Keyword arguments passed to ``arosics``.

        Reference:
            https://pypi.org/project/arosics

        Returns:
            ``xarray.DataArray``

        Example:
            >>> import geowombat as gw
            >>>
            >>> # Co-register a single image to a reference image
            >>> with gw.open('target.tif') as tar, gw.open('reference.tif') as ref:
            >>>     results = gw.coregister(tar, ref, q=True, ws=(512, 512), max_shift=3, CPUs=4)
            >>>
            >>> # or
            >>>
            >>> results = gw.coregister('target.tif', 'reference.tif', q=True, ws=(512, 512), max_shift=3, CPUs=4)
        """

        import geowombat as gw_

        if not AROSICS_INSTALLED:

            logger.exception('\nAROSICS must be installed to co-register data.\nSee https://pypi.org/project/arosics for details')
            raise NameError

        if isinstance(reference, str):

            if not os.path.isfile(reference):

                logger.exception('  The reference file does not exist.')
                raise OSError

            with gw_.open(reference) as reference:
                pass

        if isinstance(target, str):

            if not os.path.isfile(target):

                logger.exception('  The target file does not exist.')
                raise OSError

            with gw_.open(target) as target:
                pass

        cr = arosics.COREG(reference.filename,
                           target.filename,
                           **kwargs)

        try:
            cr.calculate_spatial_shifts()
        except:

            logger.warning('  Could not co-register the data.')
            return target

        shift_info = cr.correct_shifts()

        left = shift_info['updated geotransform'][0]
        top = shift_info['updated geotransform'][3]

        transform = (target.gw.cellx, 0.0, left, 0.0, -target.gw.celly, top)

        target.attrs['transform'] = transform

        data = shift_info['arr_shifted'].transpose(2, 0, 1)

        ycoords = np.linspace(top-target.gw.cellyh,
                              top-target.gw.cellyh-(data.shape[1] * target.gw.celly),
                              data.shape[1])

        xcoords = np.linspace(left+target.gw.cellxh,
                              left+target.gw.cellxh+(data.shape[2] * target.gw.cellx),
                              data.shape[2])

        return xr.DataArray(data=da.from_array(data,
                                               chunks=(target.gw.band_chunks,
                                                       target.gw.row_chunks,
                                                       target.gw.col_chunks)),
                            dims=('band', 'y', 'x'),
                            coords={'band': target.band.values.tolist(),
                                    'y': ycoords,
                                    'x': xcoords},
                            attrs=target.attrs)
