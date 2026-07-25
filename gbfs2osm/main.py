import datetime
import importlib.metadata
import logging
import xml.etree.ElementTree as ET
from enum import StrEnum
from typing import Any
from cachier import cachier

import requests
import time
import typer
from OSMPythonTools.element import Element
from OSMPythonTools.overpass import Overpass, OverpassResult
from requests import Response
from retry import retry
from rich.logging import RichHandler
from rich.progress import Progress, TextColumn, BarColumn, MofNCompleteColumn, TimeRemainingColumn
from typing_extensions import Annotated
from urllib.error import HTTPError

app = typer.Typer(name="gbfs2osm", no_args_is_help=True,
                  help="A tool to convert GBFS feeds to OSM data.")

# Simple logger that uses rich
logging.basicConfig(
    level=logging.DEBUG,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler()]
)

LOG = logging.getLogger()
logging.getLogger('OSMPythonTools').setLevel(logging.ERROR)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

version = importlib.metadata.version('gbfs2osm')

class OverwriteFields(StrEnum):
    CAPACITY = "capacity"
    NAME = "name"
    REF_GBFS = "ref:gbfs"
    NETWORK = "network"
    OPERATOR = "operator"
    BRAND = "brand"
    OPERATOR_PHONE = "operator:phone"
    OPERATOR_WEBSITE = "operator:website"
    NETWORK_WIKIDATA = "network:wikidata"
    OPERATOR_WIKIDATA = "operator:wikidata"
    FEE = "fee"
    PAYMENT_CREDIT_CARDS = "payment:credit_cards"
    PAYMENT_APP = "payment:app"
    COORDINATES = "coordinates"


@app.command()
def convert(
    operator: Annotated[str, typer.Option("--operator", help="The human-readable name of the organization that operates the bikeshare", prompt="The human-readable name of the organization that operates the bikeshare")],
    network: Annotated[str, typer.Option("--network", help="The name of the bikeshare network. Refer to https://wiki.openstreetmap.org/wiki/Tag:amenity%3Dbicycle_rental for a list of some of them.", prompt="The name of the bikeshare network. Refer to https://wiki.openstreetmap.org/wiki/Tag:amenity%3Dbicycle_rental for a list of some of them.")],
    gbfs_feed_url: Annotated[str, typer.Option("--gbfs-feed-url", help="Link to the GBFS endpoint. Example: https://gbfs.velobixi.com/gbfs/2-2/gbfs.json",
                                               prompt="Link to the GBFS endpoint.")] = "https://gbfs.velobixi.com/gbfs/2-2/gbfs.json",
    output_file: Annotated[str, typer.Option("--output-file", help="Path to the output OSM file.", prompt="Path to the output OSM file.")] = "output.osm",
    network_wikidata_id: Annotated[str, typer.Option("--network-wikidata-id", help="Wikidata ID of the bikeshare network. This is used to set the wikidata tag on the nodes. Example: Q386")] = None,
    operator_wikidata_id: Annotated[str, typer.Option("--operator-wikidata-id", help="Wikidata ID of the bikeshare operator. This is used to set the wikidata tag on the nodes. Example: Q386")] = None,
    overwrites: Annotated[list[OverwriteFields], typer.Option("--overwrite",  help="Overwrite existing tags in OSM nodes. If not specified, only the 'capacity' tag will be overwritten.", show_choices=True, metavar="FIELD")] = [OverwriteFields.CAPACITY, OverwriteFields.REF_GBFS],
):
    """
    Convert a GBFS feed to OSM data.
    """
    LOG.info(f"Fetching GBFS information at {gbfs_feed_url}")
    gbfs_data = get(gbfs_feed_url).json()
    gbfs_station_url = list(filter(lambda feed: feed['name'] == 'station_information', gbfs_data['data']['en']['feeds']))[0]['url']
    gbfs_system_url = list(filter(lambda feed: feed['name'] == 'system_information', gbfs_data['data']['en']['feeds']))[0]['url']

    # Get system name
    response = get(gbfs_system_url).json()
    system_id = response['data']['system_id']
    if not network:
        LOG.warning(f"No network name provided, using system_id: {system_id} as network name.")
        network = system_id
    if not operator:
        operator = response['data']['operator']
    if not operator:
        LOG.error("No operator name provided and the GBFS feed does not provide one. Please provide the --operator option or ensure the GBFS feed contains an operator name.")
        raise typer.Exit(code=1)
    phone_number = response['data'].get('phone_number')
    url = response['data'].get('url')

    response = get(gbfs_station_url).json()
    gbfs_station_data = [station for station in response['data']['stations'] if station.get('is_virtual_station', False) is False]
    LOG.info(f"Found {len(gbfs_station_data)} stations in the GBFS feed.")

    root = ET.Element("osm", version="0.6", generator=f"gbfs2osm {version}")

    #api = Overpass(endpoint="https://overpass.private.coffee/api/")
    api = Overpass(endpoint="https://overpass-api.de/api/")

    number_of_existing_nodes = 0

    min_longitude = min(station['lon'] for station in gbfs_station_data if station['lon'] != 0) - 0.0005
    max_longitude = max(station['lon'] for station in gbfs_station_data if station['lon'] != 0) + 0.0005
    min_latitude = min(station['lat'] for station in gbfs_station_data if station['lat'] != 0) - 0.0005
    max_latitude = max(station['lat'] for station in gbfs_station_data if station['lat'] != 0) + 0.0005

    LOG.info(f"Bounding box of stations: {min_latitude}, {min_longitude}, {max_latitude}, {max_longitude}")
    LOG.info("Fetching existing stations in Overpass")
    results: OverpassResult = query_stations(api, min_latitude, min_longitude, max_latitude, max_longitude)

    nodes: list[Element] = results.nodes()

    LOG.info(f"Found {len(nodes)} existing nodes in OpenStreetMap")

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        ) as progress:
        task = progress.add_task("Processing stations...", total=len(gbfs_station_data))
        for i, station in enumerate(gbfs_station_data):

            existing_node, distance = find_closest_node(station['lat'], station['lon'], nodes)
            if distance < 20:
                number_of_existing_nodes += 1

            if OverwriteFields.COORDINATES in overwrites and existing_node:
                lat = str(station['lat'])
                lon = str(station['lon'])
            else:
                lat = str(existing_node.lat() if existing_node else station['lat'])
                lon = str(existing_node.lon() if existing_node else station['lon'])

            node = ET.SubElement(root, "node",
                                lat=lat,
                                lon=lon,
                                id=str(existing_node.id() if existing_node else -i - 1),
                                version=str(int(existing_node._json.get('version')) + 1) if existing_node and existing_node._json.get('version') else "1")
            if existing_node:
                for tag_key in existing_node.tags():
                        ET.SubElement(node, "tag", k=tag_key, v=existing_node.tag(tag_key))

            write_tag(node, key="bicycle_rental", value="docking_station", overwrites=overwrites)
            write_tag(node, key="amenity", value="bicycle_rental", overwrites=overwrites)
            write_tag(node, key="name", value=station['name'].replace('  ', ' ').strip(), overwrites=overwrites)
            write_tag(node, key="ref:gbfs", value=f"{system_id}:{station['station_id']}", overwrites=overwrites)
            write_tag(node, key="network", value=network, overwrites=overwrites)
            write_tag(node, key="operator", value=operator, overwrites=overwrites)
            write_tag(node, key="brand", value=operator, overwrites=overwrites)
            write_tag(node, key="operator:phone", value=phone_number, overwrites=overwrites)
            write_tag(node, key="operator:website", value=url, overwrites=overwrites)
            write_tag(node, key="network:wikidata", value=network_wikidata_id, overwrites=overwrites)
            write_tag(node, key="operator:wikidata", value=operator_wikidata_id, overwrites=overwrites)
            write_tag(node, key="fee", value="yes", overwrites=overwrites)
            if "CREDITCARD" in station.get('rental_methods', []):
                write_tag(node, key="payment:credit_cards", value="yes", overwrites=overwrites)
            if "PHONE" in station.get('rental_methods', []):
                write_tag(node, key="payment:app", value="yes", overwrites=overwrites)

            if 'capacity' in station:
                if int(station['capacity']) == 0:
                    LOG.warning(f"Station {station['name']} ({station.get('station_id')}) has a capacity of 0. It is probably out of service. Skipping it entirely")
                    root.remove(node)
                    #continue
                write_tag(node, key="capacity", value=str(station['capacity']), overwrites=overwrites)

            progress.advance(task, advance=1)

    LOG.info(f"List of fields that were overwritten if they already existed: {', '.join(overwrites)}")
    LOG.info(f"Found {number_of_existing_nodes} existing nodes in OpenStreetMap. They have been updated.")
    LOG.info(f"Writing {output_file}...")
    tree = ET.ElementTree(root)
    ET.indent(tree)
    tree.write(output_file, encoding="utf-8", xml_declaration=True)

    LOG.info("Conversion complete!")


@cachier(stale_after=datetime.timedelta(days=3))
def query_stations(api, min_latitude, min_longitude, max_latitude, max_longitude) -> Any:
    try:
        return api.query(f'''
node
  ["amenity"="bicycle_rental"]
  ({min_latitude},{min_longitude},{max_latitude},{max_longitude});
out body;
''')
    except HTTPError as e:
        print(e.response.text)
        raise e

def write_tag(node: ET.Element, key: str, value: str, overwrites: list[OverwriteFields]) -> None:
    """
    Write a tag to the node if it is not already present or if it is in the overwrite list.
    """
    if value == None:
        return

    if key in overwrites:
        # If the key is in the overwrites list, we overwrite it.
        for tag in node.findall(f'tag[@k="{key}"]'):
            node.remove(tag)
    if not node.findall(f'tag[@k="{key}"]'):
        ET.SubElement(node, "tag", k=key, v=value)


def get(url: str, **kwargs) -> Response:
    """
    Make a GET request to the specified URL.
    """
    try:
        headers = kwargs.pop('headers', {})
        headers.update({'User-Agent': f"gbfs2osm {version}"})
        response = requests.get(url, **kwargs)
        response.raise_for_status()
        return response
    except HTTPError as e:
        LOG.error(f"HTTP error occurred: {e.response.text}")
        raise e


def find_closest_node(lat: float, lon: float, nodes: list[Element]) -> tuple[Element, float]:
    """
    Find the closest node to the specified latitude and longitude.
    """
    closest_node = None
    min_distance = float('inf')
    for node in nodes:
        distance = haversine(node.lat(), node.lon(), lat, lon)
        if distance < min_distance:
            min_distance = distance
            closest_node = node
    return closest_node, min_distance


from math import radians, sin, cos, sqrt, atan2
EARTH_RADIUS = 6_371_000  # meters

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance between two points in meters."""

    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (
        sin(dlat / 2) ** 2
        + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    )

    c = 2 * atan2(sqrt(a), sqrt(1 - a))

    return EARTH_RADIUS * c


app()
