import importlib.metadata
import logging
import xml.etree.ElementTree as ET
from enum import StrEnum

import requests
import typer
from OSMPythonTools.element import Element
from OSMPythonTools.overpass import Overpass, OverpassResult
from requests import HTTPError, Response
from rich.logging import RichHandler
from rich.progress import Progress, TextColumn, BarColumn, MofNCompleteColumn, TimeRemainingColumn
from typing_extensions import Annotated

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


@app.command()
def convert(
    operator: Annotated[str, typer.Option("--operator", help="The human-readable name of the organization that operates the bikeshare", prompt="The human-readable name of the organization that operates the bikeshare")],
    network: Annotated[str, typer.Option("--network", help="The name of the bikeshare network. Refer to https://wiki.openstreetmap.org/wiki/Tag:amenity%3Dbicycle_rental for a list of some of them.", prompt="The name of the bikeshare network. Refer to https://wiki.openstreetmap.org/wiki/Tag:amenity%3Dbicycle_rental for a list of some of them.")],
    gbfs_feed_url: Annotated[str, typer.Option("--gbfs-feed-url", help="Link to the GBFS endpoint. Example: https://gbfs.velobixi.com/gbfs/2-2/gbfs.json",
                                               prompt="Link to the GBFS endpoint.")] = "https://gbfs.velobixi.com/gbfs/2-2/gbfs.json",
    output_file: Annotated[str, typer.Option("--output-file", help="Path to the output OSM file.", prompt="Path to the output OSM file.")] = "output.osm",
    use_short_name_for_station_id: Annotated[bool, typer.Option("--use-short-name-for-station-id", help="Use the station's short name as station_id for the ref:gbfs tag. Some bikeshare systems (like Bixi) use changing station_id, and the real station ID is short_name.")] = False,
    network_wikidata_id: Annotated[str, typer.Option("--network-wikidata-id", help="Wikidata ID of the bikeshare network. This is used to set the wikidata tag on the nodes. Example: Q386")] = None,
    operator_wikidata_id: Annotated[str, typer.Option("--operator-wikidata-id", help="Wikidata ID of the bikeshare operator. This is used to set the wikidata tag on the nodes. Example: Q386")] = None,
    overwrites: Annotated[list[OverwriteFields], typer.Option("--overwrite",  help="Overwrite existing tags in OSM nodes. If not specified, only the 'capacity' tag will be overwritten.", show_choices=True, metavar="FIELD")] = [OverwriteFields.CAPACITY, OverwriteFields.REF_GBFS],
):
    """
    Convert a GBFS feed to OSM data.
    """
    # time.sleep(0.4)
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
    gbfs_station_data = response['data']['stations']

    root = ET.Element("osm", version="0.6", generator=f"gbfs2osm {version}")

    api = Overpass()

    number_of_existing_nodes = 0

    with Progress(TextColumn("[task.description]{task.description}"), BarColumn(), MofNCompleteColumn(), TimeRemainingColumn(), TextColumn("{task.fields[status]}")) as progress:
        task = progress.add_task("Processing stations", total=len(gbfs_station_data), status="")
        for i, station in enumerate(gbfs_station_data):
            progress.update(task, advance=0, status=station['name'])
            existing_node = None
            # First, we need to check if the station is already in the OSM database.
            # We use the Overpass API to check if there is node near the station's coordinates.
            results: OverpassResult = api.query(f'node(around:20, {station['lat']}, {station['lon']})["amenity"="bicycle_rental"];out;')

            nodes: list[Element] = results.nodes()
            if nodes:
                existing_node = find_closest_node(station['lat'], station['lon'], nodes)
                if len(nodes) > 1:
                    LOG.warning(f"{len(nodes)} nodes already in OpenStreetMap found near {station['name']} ({station['lat']}, {station['lon']}). Using node with ID {existing_node.id()} because it's the closest. However, a cleanup should be performed to remove duplicates before running this tool.")
                number_of_existing_nodes += 1

            node = ET.SubElement(root, "node",
                                 lat=str(existing_node.lat() if existing_node else station['lat']),
                                 lon=str(existing_node.lon() if existing_node else station['lon']),
                                 id=str(existing_node.id() if existing_node else -i - 1),
                                 version=str(int(existing_node._json.get('version')) + 1) if existing_node and existing_node._json.get('version') else "1")
            if existing_node:
                for tag_key in existing_node.tags():
                    if tag_key not in ['capacity']:
                        ET.SubElement(node, "tag", k=tag_key, v=existing_node.tag(tag_key))

            write_tag(node, key="bicycle_rental", value="docking_station", overwrites=overwrites)
            write_tag(node, key="amenity", value="bicycle_rental", overwrites=overwrites)
            write_tag(node, key="name", value=station['name'].replace('  ', ' ').strip(), overwrites=overwrites)
            write_tag(node, key="ref:gbfs", value=f"{system_id}:{station['short_name'] if use_short_name_for_station_id else station['station_id']}", overwrites=overwrites)
            write_tag(node, key="network", value=network, overwrites=overwrites)
            write_tag(node, key="operator", value=operator, overwrites=overwrites)
            write_tag(node, key="brand", value=operator, overwrites=overwrites)
            write_tag(node, key="operator:phone", value=phone_number, overwrites=overwrites)
            write_tag(node, key="operator:website", value=url, overwrites=overwrites)
            write_tag(node, key="network:wikidata", value=network_wikidata_id, overwrites=overwrites)
            write_tag(node, key="operator:wikidata", value=operator_wikidata_id, overwrites=overwrites)

            if 'capacity' in station:
                write_tag(node, key="capacity", value=str(station['capacity']), overwrites=overwrites)

            progress.update(task, advance=1, status=station['name'])

    LOG.info(f"List of fields that were overwritten if they already existed: {', '.join(overwrites)}")
    LOG.info(f"Found {number_of_existing_nodes} existing nodes in OpenStreetMap.")
    LOG.info(f"Writing {output_file}...")
    tree = ET.ElementTree(root)
    ET.indent(tree)
    tree.write(output_file, encoding="utf-8", xml_declaration=True)

    LOG.info("Conversion complete!")


def write_tag(node: ET.Element, key: str, value: str, overwrites: list[OverwriteFields]) -> None:
    """
    Write a tag to the node if it is not already present or if it is in the overwrite list.
    """
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


def find_closest_node(lat: float, lon: float, nodes: list[Element]) -> Element:
    """
    Find the closest node to the specified latitude and longitude.
    """
    closest_node = None
    min_distance = float('inf')
    for node in nodes:
        distance = ((node.lat() - lat) ** 2 + (node.lon() - lon) ** 2) ** 0.5
        if distance < min_distance:
            min_distance = distance
            closest_node = node
    return closest_node


app()
