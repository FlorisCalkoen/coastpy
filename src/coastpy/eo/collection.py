import abc
import logging
from collections.abc import Callable
from typing import Any

import geopandas as gpd
import numpy as np
import odc.geo.geobox
import odc.geo.geom
import odc.stac
import pandas as pd
import pyproj
import pystac
import pystac.item
import pystac_client
import rioxarray  # noqa
import stac_geoparquet
import xarray as xr

from coastpy.eo.indices import calculate_indices
from coastpy.eo.mask import apply_mask, geometry_mask, nodata_mask, numeric_mask
from coastpy.stac.utils import read_snapshot
from coastpy.utils.xarray import combine_by_first

# NOTE: currently all NODATA management is removed because we mask nodata after loading it by default.


class ImageCollection:
    """
    A generic class to manage image collections from a STAC-based catalog.
    """

    def __init__(
        self,
        catalog_url: str,
        collection: str,
        stac_cfg: dict | None = None,
    ):
        self.catalog_url = catalog_url
        self.collection = collection
        self.catalog = pystac_client.Client.open(self.catalog_url)

        # Configuration
        self.search_params = {}
        self.load_params = {}
        self.bands = []
        self.spectral_indices = []
        self.percentile = None
        self.stac_cfg = stac_cfg or {}

        # Masking options
        self.geometry_mask = None
        self.nodata_mask = False
        self.value_mask = None

        # Internal state
        self.geometry = None
        self.stac_items = None
        self.dataset = None

    def search(
        self,
        roi: gpd.GeoDataFrame,
        datetime_range: str,
        query: dict | None = None,
        filter_function: Callable[[list[pystac.Item]], list[pystac.Item]] | None = None,
    ) -> "ImageCollection":
        """
        Search the catalog for items and optionally apply a filter function.

        Args:
            roi (gpd.GeoDataFrame): Region of interest.
            datetime_range (str): Temporal range in 'YYYY-MM-DD/YYYY-MM-DD'.
            query (dict, optional): Additional query parameters for search.
            filter(Callable, optional): A custom function to filter/sort items.
                Accepts and returns a list of pystac.Items.

        Returns:
            ImageCollection: Updated instance with items populated.
        """
        geom = roi.to_crs(4326).geometry.item()
        self.search_params = {
            "collections": self.collection,
            "intersects": geom,
            "datetime": datetime_range,
            "query": query,
        }
        self.geometry = odc.geo.geom.Geometry(geom, crs=roi.crs)

        # Perform the actual search
        logging.info(f"Executing search with params: {self.search_params}")
        search = self.catalog.search(**self.search_params)
        self.stac_items = list(search.items())

        # Check if items were found
        if not self.stac_items:
            msg = "No items found for the given search parameters."
            raise ValueError(msg)

        # Apply the filter function if provided
        # move to composite
        if filter_function:
            try:
                logging.info("Applying custom filter function.")
                self.stac_items = filter_function(self.stac_items)
            except Exception as e:
                msg = f"Error in filter_function: {e}"
                raise RuntimeError(msg)  # noqa: B904

        return self

    def load(
        self,
        bands: list[str],
        percentile: int | None = None,
        spectral_indices: list[str] | None = None,
        mask_nodata: bool = True,
        chunks: dict[str, int | str] | None = None,
        groupby: str = "solar_day",
        resampling: str | dict[str, str] | None = None,
        dtype: np.dtype | str | None = None,
        crs: str | int = "utm",
        resolution: float | int | None = None,
        pool: int | None = None,
        preserve_original_order: bool = False,
        progress: bool | None = None,
        fail_on_error: bool = True,
        geobox: odc.geo.geobox.GeoBox | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        geopolygon: dict | None = None,
        lon: tuple[float, float] | None = None,
        lat: tuple[float, float] | None = None,
        x: tuple[float, float] | None = None,
        y: tuple[float, float] | None = None,
        like: xr.Dataset | None = None,
        patch_url: str | None = None,
        stac_cfg: dict | None = None,
        anchor: str | None = None,
    ) -> "ImageCollection":
        """
        Configure parameters for loading data via odc.stac.load.

        Args:
            bands (list[str]): Bands to load (required).
            percentile (int | None): Percentile for compositing (e.g., 50 for median).
            spectral_indices (list[str] | None): List of spectral indices to compute.
            mask_nodata (bool): Mask no-data values. Defaults to True.
            resolution (float | int | None): Pixel resolution in CRS units. Defaults to 10.
            crs (str | int): Coordinate reference system. Defaults to 'utm'.
            geobox (GeoBox | None): Exact region, resolution, and CRS to load. Overrides other extent parameters.
            bbox, geopolygon, lon, lat, x, y: Optional parameters to define extent.
            Additional args: Parameters passed to odc.stac.load.

        Returns:
            ImageCollection: Updated instance.
        """
        if not bands:
            raise ValueError("Argument `bands` is required.")

        if percentile is not None and not (0 <= percentile <= 100):
            raise ValueError("`percentile` must be between 0 and 100.")

        self.bands = bands
        self.spectral_indices = spectral_indices
        self.percentile = percentile
        self.mask_nodata = mask_nodata

        # Geobox creation
        if geobox is None and resolution and self.geometry is not None:
            geobox = self._create_geobox(
                geometry=self.geometry, resolution=resolution, crs=crs
            )
            resolution = None  # Let geobox handle resolution

        # Assemble load parameters
        self.load_params = {
            "chunks": chunks,
            "groupby": groupby,
            "resampling": resampling,
            "dtype": dtype,
            "resolution": resolution,
            "pool": pool,
            "preserve_original_order": preserve_original_order,
            "progress": progress,
            "fail_on_error": fail_on_error,
            "geobox": geobox,
            "bbox": bbox,
            "geopolygon": geopolygon,
            "lon": lon,
            "lat": lat,
            "x": x,
            "y": y,
            "like": like,
            "patch_url": patch_url,
            "stac_cfg": self.stac_cfg or stac_cfg,
            "anchor": anchor,
        }

        return self

    def mask(
        self,
        geometry: odc.geo.geom.Geometry | None = None,
        nodata: bool = True,
        values: list[int] | None = None,
    ) -> "ImageCollection":
        """
        Configure masking options.

        Args:
            geometry (odc.geo.geom.Geometry | None): Geometry to mask data within.
            nodata (bool): Whether to apply a nodata mask.
            values (List[float | int] | None): Specific values to mask.

        Returns:
            ImageCollection: Updated instance with masking options configured.
        """
        self.geometry_mask = geometry
        self.nodata_mask = nodata
        self.value_mask = values
        return self

    def _load(self) -> xr.Dataset:
        """
        Load data using odc.stac.load.

        Returns:
            xr.Dataset: Loaded dataset.
        """
        if not self.stac_items:
            raise ValueError("No STAC items found. Perform a search first.")

        # Adjust groupby for percentile-based compositing
        if self.percentile:
            self.load_params["groupby"] = "id"

        # Fallback to bbox if no spatial bounds are provided
        if (
            not self.load_params.get("geobox")
            and not self.load_params.get("bbox")
            and not self.load_params.get("geopolygon")
            and not self.load_params.get("like")
        ):
            bbox = tuple(self.search_params["intersects"].bounds)
            self.load_params["bbox"] = bbox

        # Call odc.stac.load
        ds = odc.stac.load(
            self.stac_items,
            bands=self.bands,
            **self.load_params,
        )

        # Add metadata if time matches item count
        if ds.sizes["time"] == len(self.stac_items):
            ds = self._add_metadata_from_stac(self.stac_items, ds)

        return ds

    @classmethod
    def _create_geobox(
        cls, geometry: odc.geo.geom.Geometry, resolution=None, crs=None, shape=None
    ):
        """
        Create a GeoBox from the given geometry.

        Args:
            geometry (odc.geo.geom.Geometry): Geometry of the region of interest.
            resolution (float, tuple, or None): Pixel resolution (units of CRS). Defaults to None.
            crs (str or int, optional): CRS of the GeoBox. Defaults to the geometry's CRS.
            shape (tuple, optional): Shape (height, width) of the output GeoBox.

        Returns:
            GeoBox: Constructed GeoBox, or None if resolution is not provided.
        """
        if crs is None:
            crs = geometry.crs  # Default to geometry CRS

        if resolution is not None:
            return odc.geo.geobox.GeoBox.from_geopolygon(
                geometry, resolution=resolution, crs=crs, shape=shape
            )

        return None  # Let bbox or other parameters handle extent

    @classmethod
    def _add_metadata_from_stac(
        cls, items: list[pystac.Item], ds: xr.Dataset
    ) -> xr.Dataset:
        """
        Attach metadata from STAC items to the dataset as coordinates.
        """
        if len(items) != ds.sizes["time"]:
            raise ValueError("Mismatch between STAC items and dataset time dimension.")

        mgrs_tiles = [i.properties["s2:mgrs_tile"] for i in items]
        cloud_cover = [i.properties["eo:cloud_cover"] for i in items]
        rel_orbits = [i.properties["sat:relative_orbit"] for i in items]
        stac_ids = [i.id for i in items]

        # Assign metadata as coordinates
        ds = ds.assign_coords({"stac_id": ("time", stac_ids)})
        ds = ds.assign_coords({"s2:mgrs_tile": ("time", mgrs_tiles)})
        ds = ds.assign_coords({"eo:cloud_cover": ("time", cloud_cover)})
        ds = ds.assign_coords({"sat:relative_orbit": ("time", rel_orbits)})

        return ds

    def _apply_masks(self, ds: xr.DataArray | xr.Dataset) -> xr.DataArray | xr.Dataset:
        # Apply pre-load masks
        if self.geometry_mask:
            crs = ds.rio.crs
            if not crs:
                msg = "Dataset must have a CRS to apply geometry mask."
                raise ValueError(msg)
            geometry = self.geometry_mask.to_crs(crs)
            ds = geometry_mask(ds, geometry)
            return ds

        if self.nodata_mask:
            mask = nodata_mask(ds)
            ds = apply_mask(ds, mask)

        if self.value_mask:
            mask = numeric_mask(ds, self.value_mask)
            ds = apply_mask(ds, mask)

        return ds

    def add_spectral_indices(self, indices: list[str]) -> "ImageCollection":
        """
        Add spectral indices to the current dataset.

        Args:
            indices (List[str]): Spectral indices to calculate.

        Returns:
            ImageCollection: Updated ImageCollection with spectral indices.
        """
        self.spectral_indices = indices
        return self

    @classmethod
    def _composite(
        cls,
        ds: xr.DataArray | xr.Dataset,
        percentile: int,
    ) -> xr.DataArray | xr.Dataset:
        """
        Generate a composite dataset using median or percentile methods,
        respecting metadata and sampling constraints.

        Args:
            ds (xr.DataArray | xr.Dataset): The input dataset.
            percentile (int): Percentile value (50 for median).

        Returns:
            xr.DataArray | xr.Dataset: Composite dataset with a time dimension.
        """
        try:
            # Step 1: Create a combined group key
            ds = ds.assign(
                group_key=(
                    xr.apply_ufunc(
                        lambda tile, orbit: tile + "_" + str(orbit),
                        ds["s2:mgrs_tile"],
                        ds["sat:relative_orbit"],
                        vectorize=True,
                    )
                )
            )

            # Step 2: Sort by cloud coverage
            ds_sorted = ds.sortby("eo:cloud_cover")

            # Step 3: Group by the combined key
            grouped = ds_sorted.groupby("group_key")

            # Step 4: Sample and compute the composite
            def sample_and_aggregate(group):
                if percentile == 50:
                    return group.median(dim="time", skipna=True, keep_attrs=True)
                else:
                    return group.quantile(
                        percentile / 100, dim="time", skipna=True, keep_attrs=True
                    )

            composite = grouped.map(sample_and_aggregate)
            datasets = [
                composite.isel(group_key=i) for i in range(composite.sizes["group_key"])
            ]
            collapsed = combine_by_first(datasets)

            # Step 5: Compute temporal metadata
            def compute_metadata(grouped):
                group_metadata = []
                for _, group in grouped:
                    datetimes = group.time.to_series().sort_values()
                    start_datetime = datetimes.min().isoformat()
                    end_datetime = datetimes.max().isoformat()
                    avg_interval = datetimes.diff().mean()
                    n_obs = len(datetimes)

                    group_metadata.append(
                        {
                            "start_datetime": start_datetime,
                            "end_datetime": end_datetime,
                            "avg_interval": avg_interval,
                            "n_obs": n_obs,
                        }
                    )
                return group_metadata

            group_metadata = compute_metadata(grouped)

            # Aggregate global metadata
            start_datetime = min(item["start_datetime"] for item in group_metadata)
            end_datetime = max(item["end_datetime"] for item in group_metadata)
            avg_intervals = [item["avg_interval"] for item in group_metadata]
            avg_interval = f"{pd.Series(avg_intervals).mean().days} days"  # type: ignore
            avg_obs = np.mean([item["n_obs"] for item in group_metadata])

            # Add the `time` dimension and metadata
            collapsed = collapsed.expand_dims(time=[pd.Timestamp(start_datetime)])
            collapsed = collapsed.assign_coords(
                {
                    "time": ("time", [pd.Timestamp(start_datetime)]),
                    "start_datetime": ("time", [pd.Timestamp(start_datetime)]),
                    "end_datetime": ("time", [pd.Timestamp(end_datetime)]),
                }
            )

            # Update global attributes for composite metadata
            collapsed.attrs.update(
                {
                    "eo:cloud_cover": ds_sorted["eo:cloud_cover"].mean().item(),
                    "composite:determination_method": "median"
                    if percentile == 50
                    else "percentile",
                    "composite:percentile": percentile,
                    "composite:groups": list(ds_sorted.group_key.to_series().unique()),
                    "composite:avg_obs": avg_obs,
                    "composite:stac_ids": list(ds_sorted.stac_id.values),
                    "composite:avg_interval": avg_interval,
                    "composite:summary": (
                        f"Composite dataset created by grouping on ['s2:mgrs_tile', 'sat:relative_orbit'], using a "
                        f"{'median' if percentile == 50 else f'{percentile}th percentile'} method, "
                        f"sorted by 'eo:cloud_cover' with an average of {avg_obs} images per group."
                    ),
                }
            )

            return collapsed

        except Exception as e:
            raise RuntimeError(f"Failed to generate composite: {e}") from e

    def composite(
        self,
        percentile: int = 50,
        filter_function: Callable[[list[pystac.Item]], list[pystac.Item]] | None = None,
    ) -> "ImageCollection":
        """
        Apply a composite operation to the dataset based on the given percentile.

        Args:
            percentile (int): Percentile to calculate (e.g., 50 for median).
                            Values range between 0 and 100.
            filter_function (Callable, optional): A custom function to filter/sort items.
                                                Accepts and returns a list of pystac.Items.

        Returns:
            ImageCollection: Composited dataset.

        Raises:
            ValueError: If percentile is not between 0 and 100 or if no STAC items are found.
            RuntimeError: If an error occurs in the filter_function.
        """
        if not (0 <= percentile <= 100):
            msg = "Percentile must be between 0 and 100."
            raise ValueError(msg)
        logging.info(f"Applying {percentile}th percentile composite.")

        if not self.stac_items:
            raise ValueError("No STAC items found. Perform a search first.")

        if filter_function:
            try:
                logging.info("Applying custom filter function.")
                self.stac_items = filter_function(self.stac_items)
            except Exception as e:
                msg = f"Error in filter_function: {e}"
                raise RuntimeError(msg) from e

        self.percentile = percentile
        return self

    def execute(self) -> xr.DataArray | xr.Dataset:
        if self.stac_items is None:
            search = self.catalog.search(**self.search_params)
            self.stac_items = list(search.items())

        if self.dataset is None:
            self.dataset = self._load()

        if self.geometry_mask or self.nodata_mask or self.value_mask:
            self.dataset = self._apply_masks(self.dataset)

        if self.percentile:
            self.dataset = self._composite(
                ds=self.dataset,
                percentile=self.percentile,
            )

        if self.spectral_indices:
            if isinstance(self.dataset, xr.DataArray):
                try:
                    ds = self.dataset.to_dataset("band")
                    self.dataset = ds
                except Exception as e:
                    msg = "Cannot convert DataArray to Dataset: {e}"
                    raise ValueError(msg) from e
                msg = "Spectral indices not implemented for DataArray."
                raise NotImplementedError(msg)

            self.dataset = calculate_indices(self.dataset, self.spectral_indices)

        return self.dataset


class S2Collection(ImageCollection):
    """
    A class to manage Sentinel-2 collections from the Planetary Computer catalog.
    """

    def __init__(
        self,
        catalog_url: str = "https://planetarycomputer.microsoft.com/api/stac/v1",
        collection: str = "sentinel-2-l2a",
    ):
        stac_cfg = {
            "sentinel-2-l2a": {
                "assets": {
                    "*": {"data_type": None, "nodata": np.nan},
                    "SCL": {"data_type": None, "nodata": np.nan},
                    "visual": {"data_type": None, "nodata": np.nan},
                },
            },
            "*": {"warnings": "ignore"},
        }

        super().__init__(catalog_url, collection, stac_cfg)


class TileCollection:
    """
    A generic class to manage tile collections from a STAC-based catalog.
    """

    def __init__(
        self,
        catalog_url: str,
        collection: str,
        stac_cfg: dict | None = None,
    ):
        self.catalog_url = catalog_url
        self.collection = collection
        self.catalog = pystac_client.Client.open(self.catalog_url)

        # Configuration
        self.search_params = {}
        self.bands = []
        self.load_params = {}
        self.stac_cfg = stac_cfg or {}

        # Internal state
        self.items = None
        self.dataset = None

    @abc.abstractmethod
    def search(self, roi: gpd.GeoDataFrame) -> "TileCollection":
        """
        Search for DeltaDTM items based on a region of interest.
        """

    def load(
        self,
        chunks: dict[str, int | str] | None = None,
        resampling: str | dict[str, str] | None = None,
        dtype: np.dtype | str | None = None,
        crs: str | int | None = None,
        resolution: float | int | None = None,
        pool: int | None = None,
        preserve_original_order: bool = False,
        progress: bool | None = None,
        fail_on_error: bool = True,
        geobox: dict | None = None,
        like: xr.Dataset | None = None,
        patch_url: str | None = None,
        dst_crs: Any | None = None,
    ) -> "TileCollection":
        """
        Configure loading parameters.

        Args:
            Additional args: Parameters for odc.stac.load.

        Returns:
            DeltaDTMCollection: Updated instance.
        """
        self.dst_crs = dst_crs

        self.load_params = {
            "chunks": chunks or {},
            "resampling": resampling,
            "dtype": dtype,
            "crs": crs,
            "resolution": resolution,
            "pool": pool,
            "preserve_original_order": preserve_original_order,
            "progress": progress,
            "fail_on_error": fail_on_error,
            "geobox": geobox,
            "like": like,
            "patch_url": patch_url,
        }
        return self

    def _load(self) -> xr.Dataset:
        """
        Internal method to load data using odc.stac.
        """
        if not self.items:
            msg = "No items found. Perform a search first."
            raise ValueError(msg)

        bbox = tuple(self.search_params["intersects"].bounds)

        ds = odc.stac.load(
            self.items,
            bbox=bbox,
            **self.load_params,
        ).squeeze()

        if self.dst_crs and (
            pyproj.CRS.from_user_input(self.dst_crs).to_epsg() != ds.rio.crs.to_epsg()
        ):
            ds = ds.rio.reproject(self.dst_crs)
            ds = ds.odc.reproject(self.dst_crs, resampling="cubic")

        return ds

    def _post_process(self, ds: xr.Dataset) -> xr.Dataset:
        """Post-process the dataset."""
        return ds

    def execute(self) -> xr.Dataset:
        """
        Trigger the search and load process and return the dataset.
        """
        # Perform search if not already done
        if self.items is None:
            msg = "No items found. Perform a search first."
            raise ValueError(msg)

        # Perform load if not already done
        if self.dataset is None:
            logging.info("Loading dataset...")
            self.dataset = self._load()
            self.dataset = self._post_process(self.dataset)

        return self.dataset


class DeltaDTMCollection(TileCollection):
    """
    A class to manage Delta DTM collections from the CoCliCo catalog.
    """

    def __init__(
        self,
        catalog_url: str = "https://coclico.blob.core.windows.net/stac/v1/catalog.json",
        collection: str = "deltares-delta-dtm",
    ):
        super().__init__(catalog_url, collection)

    def search(self, roi: gpd.GeoDataFrame) -> "DeltaDTMCollection":
        """
        Search for DeltaDTM items based on a region of interest.
        """

        self.search_params = {
            "collections": self.collection,
            "intersects": roi.to_crs(4326).geometry.item(),
        }

        col = self.catalog.get_collection(self.collection)
        storage_options = col.extra_fields["item_assets"]["data"][
            "xarray:storage_options"
        ]
        ddtm_extents = read_snapshot(
            col,
            columns=None,
            storage_options=storage_options,
        )
        r = gpd.sjoin(ddtm_extents, roi.to_crs(ddtm_extents.crs)).drop(
            columns="index_right"
        )
        self.items = list(stac_geoparquet.to_item_collection(r))

        # Check if items were found
        if not self.items:
            msg = "No items found for the given search parameters."
            raise ValueError(msg)

        return self

    def _post_process(self, ds: xr.Dataset) -> xr.Dataset:
        """Post-process the dataset."""
        ds["data"] = ds["data"].where(ds["data"] != ds["data"].attrs["nodata"], 0)
        # NOTE: Idk if this is good practice
        ds["data"].attrs["nodata"] = np.nan
        return ds


class CopernicusDEMCollection(TileCollection):
    """
    A class to manage Copernicus DEM collections from the Planetary Computer catalog.
    """

    def __init__(
        self,
        catalog_url: str = "https://planetarycomputer.microsoft.com/api/stac/v1",
        collection: str = "cop-dem-glo-30",
    ):
        stac_cfg = {
            "cop-dem-glo-30": {
                "assets": {
                    "*": {"data_type": "int16", "nodata": -32768},
                },
                "*": {"warnings": "ignore"},
            }
        }

        super().__init__(catalog_url, collection, stac_cfg)

    def search(self, roi: gpd.GeoDataFrame) -> "CopernicusDEMCollection":
        """
        Search for Copernicus DEM items based on a region of interest.
        """
        self.search_params = {
            "collections": self.collection,
            "intersects": roi.to_crs(4326).geometry.item(),
        }

        # Perform the search
        logging.info(f"Executing search with params: {self.search_params}")
        search = self.catalog.search(**self.search_params)
        self.items = list(search.items())

        # Check if items were found
        if not self.items:
            msg = "No items found for the given search parameters."
            raise ValueError(msg)

        return self


if __name__ == "__main__":
    import logging
    import os
    from typing import Any, Literal

    import dotenv
    import fsspec
    import geopandas as gpd
    import odc
    import odc.geo.geom
    import odc.stac
    import planetary_computer as pc
    import pystac
    import shapely
    import stac_geoparquet
    import xarray as xr
    from odc.stac import configure_rio

    # from coastpy.eo.collection import S2Collection
    from coastpy.eo.filter import filter_and_sort_stac_items
    from coastpy.stac.utils import read_snapshot
    from coastpy.utils.config import configure_instance

    configure_rio(cloud_defaults=True)
    instance_type = configure_instance()

    dotenv.load_dotenv()
    sas_token = os.getenv("AZURE_STORAGE_SAS_TOKEN")
    storage_options = {"account_name": "coclico", "sas_token": sas_token}

    west, south, east, north = (4.796, 53.108, 5.229, 53.272)

    roi = gpd.GeoDataFrame(
        geometry=[shapely.geometry.box(west, south, east, north)], crs=4326
    )

    def get_coastal_zone(coastal_grid, region_of_interest):
        df = gpd.sjoin(coastal_grid, region_of_interest).drop(columns=["index_right"])
        coastal_zone = df.union_all()
        return odc.geo.geom.Geometry(coastal_zone, crs=df.crs)

    def filter_function(items):
        return filter_and_sort_stac_items(
            items,
            max_items=10,
            group_by=["s2:mgrs_tile", "sat:relative_orbit"],
            sort_by="eo:cloud_cover",
        )

    def read_coastal_grid(
        zoom: Literal[5, 6, 7, 8, 9, 10],
        buffer_size: Literal["500m", "1000m", "2000m", "5000m", "10000m", "15000m"],
        storage_options,
    ):
        """
        Load the coastal zone data layer for a specific buffer size.
        """
        coclico_catalog = pystac.Catalog.from_file(
            "https://coclico.blob.core.windows.net/stac/v1/catalog.json"
        )
        coastal_zone_collection = coclico_catalog.get_child("coastal-grid")
        if coastal_zone_collection is None:
            msg = "Coastal zone collection not found"
            raise ValueError(msg)
        item = coastal_zone_collection.get_item(f"coastal_grid_z{zoom}_{buffer_size}")
        if item is None:
            msg = f"Coastal zone item for zoom {zoom} with {buffer_size} not found"
            raise ValueError(msg)
        href = item.assets["data"].href
        with fsspec.open(href, mode="rb", **storage_options) as f:
            coastal_zone = gpd.read_parquet(f)
        return coastal_zone

    coastal_grid = read_coastal_grid(
        zoom=10, buffer_size="5000m", storage_options=storage_options
    )

    coastal_zone = get_coastal_zone(coastal_grid, roi)

    s2 = (
        S2Collection()
        .search(
            roi,
            datetime_range="2022-01-01/2023-12-31",
            query={"eo:cloud_cover": {"lt": 20}},
        )
        .load(
            # bands=["blue"],
            bands=["blue", "green", "red", "nir", "swir16"],
            # percentile=50,
            spectral_indices=["NDWI", "NDVI", "MNDWI", "NDMI"],
            # mask_nodata=True,
            # chunks={},
            patch_url=pc.sign,
            groupby="id",
            resampling={"swir16": "bilinear"},
            crs=32631,
        )
        .mask(geometry=coastal_zone, nodata=True)
        .composite(percentile=50, filter_function=filter_function)
        .execute()
    )

    s2 = s2.compute()

    print("Done")
