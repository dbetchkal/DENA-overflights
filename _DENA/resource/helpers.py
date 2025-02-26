import glob
import logging
import os
from typing import List, Optional, TYPE_CHECKING, Union

import geopandas as gpd
import pandas as pd
from tqdm import tqdm

from nps_active_space import ACTIVE_SPACE_DIR
from nps_active_space.utils import Adsb, EarlyAdsb, Microphone

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


__all__ = [
    'get_deployment',
    'get_logger',
    'get_omni_sources',
    'query_adsb',
    'query_tracks'
]


def get_deployment(unit: str, site: str, year: int, filename: str, elevation: bool = True) -> Microphone:
    """
    Obtain all metadata for a specific microphone deployment from a metadata file.

    Parameters
    ----------
    unit : str
        Four letter park service unit code E.g. 'DENA'
    site : str
        Deployment site character code. E.g. 'TRLA', '009'
    year : int
        Deployment year. YYYY
    filename : str
        Absolute path to microphone deployment metadata text file. '/path/to/metadata.txt'
    elevation : bool, default True
        If True, the microphone z value will be set to its elevation. If False, the microphone z value will be
        set to the microphone's height from the ground.

    Returns
    -------
    mic : Microphone
        A Microphone object containing the mic deployment site metadata from the specific unit/site/year combination.
    """

    print(unit, site, year)
    metadata = pd.read_csv(filename, delimiter='\t', encoding='ISO-8859-1')

    # this rather cumbersome line assures that any sites styled as '009' or '099' are correctly formatted as strings
    metadata.loc[metadata["code"].astype('str').str.len() <= 3, "code"] = metadata.loc[metadata["code"].astype('str').str.len() <= 3, "code"].apply(lambda s: str(s).zfill(3))
    
    site_meta = metadata.loc[(metadata['unit'] == unit) & (metadata['code'] == site) & (metadata['year'] == year)]

    # Microphone coordinates are stored in WGS84, epsg:4326
    mic = Microphone(
        lat=site_meta.lat.iat[0],
        lon=site_meta.long.iat[0],
        z=site_meta.elevation.iat[0] if elevation else site_meta.microphone_height.iat[0],
        name=f"{unit}{site}{year}"
    )

    return mic


def query_tracks(engine: 'Engine', start_date: str, end_date: str,
                 mask: Optional[gpd.GeoDataFrame] = None, 
                 mask_buffer_distance: Optional[int] = None) -> gpd.GeoDataFrame:
    """
    Query flight tracks from the FlightsDB for a specific date range and optional within a specific area.

    Parameters
    ----------
    engine : sqlalchemy Engine
        SQLAlchemy Engine instance for connecting to the overflights DB.
    start_date : str
        ISO date string (YYYY-mm-dd) indicating the beginning of the date range to query within
    end_date : str
        ISO date string (YYYY-mm-dd) indicating the end of the date range to query within
    mask : gpd.GeoDataFrame, default None
        Geopandas.GeoDataframe instance to spatially filter query results.

    Returns
    -------
    data : gpd.GeoDataFrame
        A GeoDataFrame of flight track points.
    """
    wheres = [f"fp.ak_datetime::date BETWEEN '{start_date}' AND '{end_date}'"]

    if mask is not None:
        if mask.crs.to_epsg() != 4326:  # If mask is not already in WGS84, project it.
            mask = mask.to_crs(epsg='4326')
        mask['dissolve_field'] = 1
        if mask_buffer_distance:
            ak_albers_mask = mask.to_crs(epsg=3338)
            mask.geometry = ak_albers_mask.buffer(mask_buffer_distance).to_crs(epsg=4326)
        mask_wkt = mask.dissolve(by='dissolve_field').squeeze()['geometry'].wkt
        wheres.append(f"ST_Intersects(geom, ST_GeomFromText('{mask_wkt}', 4326))")

    query = f"""
        SELECT
            f.flight_id as flight_id,
            fp.altitude_ft * 0.3048 as altitude_m,
            fp.ak_datetime,
            fp.geom, 
            date_trunc('hour', fp.ak_datetime) as ak_hourtime
        FROM flight_points as fp
        JOIN flights f ON f.id = fp.flight_id
        WHERE {' AND '.join(wheres)}
        ORDER BY fp.ak_datetime asc
        """

    flight_tracks = gpd.GeoDataFrame.from_postgis(query, engine, geom_col='geom', crs='epsg:4326')

    data = flight_tracks.loc[~(flight_tracks.geometry.is_empty)]
    return data


def query_adsb(adsb_path: str,  start_date: str, end_date: str,
               mask: Optional[gpd.GeoDataFrame] = None,
               mask_buffer_distance: Optional[int] = None,
               exclude_early_ADSB: Optional[bool] = False) -> Union[Adsb, EarlyAdsb]:
    """
    Query flight tracks from ADSB files for a specific date range and optional within a specific area.

    Parameters
    ----------
    adsb_path : str
        Absolute path to a directory with adsb data files to read in.
    start_date : str
        ISO date string (YYYY-mm-dd) indicating the beginning of the date range to query within
    end_date : str
        ISO date string (YYYY-mm-dd) indicating the end of the date range to query within
    mask : gpd.GeoDataFrame, default None
        Geopandas.GeoDataframe instance to spatially filter query results.

    Returns
    -------
    adsb : ADSB or EarlyADSB
        An ADSB or EarlyADSB object of flight track points.
    """

    if (int(start_date[:4]) <= 2019) & (exclude_early_ADSB == False):  # ADSB file formats changed after 2019.
        adsb_files = glob.glob(os.path.join(adsb_path, "*.txt"))
        adsb = EarlyAdsb(adsb_files)
    else:
        adsb_files = glob.glob(os.path.join(adsb_path, "*.TSV"))
        adsb = Adsb(adsb_files)
    adsb = adsb.loc[(adsb["TIME"] > start_date) & (adsb["TIME"] < end_date)]

    if mask is not None:
        if not mask.crs.to_epsg() == 4326:  # If mask is not already in WGS84, project it.
            mask = mask.to_crs(epsg='4326')
        if mask_buffer_distance:
            ak_albers_mask = mask.to_crs(epsg=3338)
            mask.geometry = ak_albers_mask.buffer(mask_buffer_distance).to_crs(epsg=4326)
        print(adsb.crs)
        adsb.set_crs(epsg='4326', inplace=True)
        adsb = gpd.clip(adsb, mask)

    adsb = adsb.loc[~(adsb.geometry.is_empty)]
    return adsb


class _TqdmStream:
    """
    A Logger Stream so Tqdm loading bars work with python loggers.
    https://github.com/tqdm/tqdm/issues/313#issuecomment-346819396
    """
    def write(cls, msg: str):
        tqdm.write(msg, end='', )
    write = classmethod(write)


def get_logger(name: str, level: str = 'INFO') -> logging.Logger:
    """
    General purpose function for creating a console logger.

    Parameters
    ----------
    name : str
        Logger name
    level : str, default INFO
        Logger message severity

    Returns
    -------
    logger : logging.Logger
        A python logger object
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    handler = logging.StreamHandler(stream=_TqdmStream)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


def get_omni_sources(lower: float, upper: float) -> List[str]:
    """
    Get a list of omni source files for tuning NMSim within a specific gain range.
    Source files are provided in the data directory for gains between -30 and +50.

    O_+005 = .5
    O_+050 = 5
    O_+500 = 50

    Parameters
    ----------
    lower: float
        The lowest gain omni source file to pull.
    upper : float
        The high gain omni source file to pull

    Returns
    -------
    A list of omni source files within the specified gain range.

    Raises
    ------
    AssertionError if the lower or upper gain bound is out of range or of the upper gain bound is lower than
    the lower gain bound.
    """
    assert -30 <= upper <= 50 and -30 <= lower <= 50 and upper >= lower, "Bounds must be ordered and between [-30, 50]."
    assert upper % .5 == 0, "Invalid upper limit. Value must be divisible by 0.5."
    assert lower % .5 == 0, "Invalid lower limit. Value must be divisible by 0.5."

    omni_source_dir = f"{ACTIVE_SPACE_DIR}\\data\\tuning"
    omni_sources = []

    for i in range(int(lower*10), int(upper*10+5), 5):
        if i < 0:
            omni_sources.append(f"{omni_source_dir}\\O_{i:04}.src")
        elif i >= 0:
            omni_sources.append(f"{omni_source_dir}\\O_+{i:03}.src")

    return omni_sources
