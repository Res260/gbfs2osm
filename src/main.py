import importlib.metadata
import logging
import xml.etree.ElementTree as ET

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


@app.command()
def convert(
    operator: Annotated[str, typer.Option("--operator", help="The human-readable name of the organization that operates the bikeshare", prompt="The human-readable name of the organization that operates the bikeshare")],
    network: Annotated[str, typer.Option("--network", help="The name of the bikeshare network. Refer to https://wiki.openstreetmap.org/wiki/Tag:amenity%3Dbicycle_rental for a list of some of them.", prompt="The name of the bikeshare network. Refer to https://wiki.openstreetmap.org/wiki/Tag:amenity%3Dbicycle_rental for a list of some of them.")],
    gbfs_feed_url: Annotated[str, typer.Option("--gbfs-feed-url", help="Link to the GBFS endpoint. Example: https://gbfs.velobixi.com/gbfs/2-2/gbfs.json",
                                               prompt="Link to the GBFS endpoint.")] = "https://gbfs.velobixi.com/gbfs/2-2/gbfs.json",
    output_file: Annotated[str, typer.Option("--output-file", help="Path to the output OSM file.", prompt="Path to the output OSM file.")] = "output.osm",
    use_short_name_for_station_id: Annotated[bool, typer.Option("--use-short-name-for-station-id", help="Use the station's short name as station_id for the ref:gbfs tag. Some bikeshare systems (like Bixi) use changing station_id, and the real station ID is short_name.")] = False
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

            if not node.findall('tag[@k="bicycle_rental"]'):
                ET.SubElement(node, "tag", k="bicycle_rental", v="docking_station")
            ET.SubElement(node, "tag", k="amenity", v="bicycle_rental")
            if not node.findall('tag[@k="name"]'):
                ET.SubElement(node, "tag", k="name", v=station['name'].replace('  ', ' ').strip())
            if not node.findall('tag[@k="ref:gbfs"]'):
                ET.SubElement(node, "tag", k="ref:gbfs", v=f"{system_id}:{station['short_name'] if use_short_name_for_station_id else station['station_id']}")
            if not node.findall('tag[@k="network"]'):
                ET.SubElement(node, "tag", k="network", v=network)
            if not node.findall('tag[@k="operator"]'):
                ET.SubElement(node, "tag", k="operator", v=operator)
            if not node.findall('tag[@k="brand"]'):
                ET.SubElement(node, "tag", k="brand", v=operator)
            if phone_number and not node.findall('tag[@k="operator:phone"]'):
                ET.SubElement(node, "tag", k="operator:phone", v=existing_node.tag('operator:phone') if existing_node and existing_node.tag('operator:phone') else phone_number)
            if url and not node.findall('tag[@k="operator:website"]'):
                ET.SubElement(node, "tag", k="operator:website", v=existing_node.tag('operator:website') if existing_node and existing_node.tag('operator:website') else url)

            if 'capacity' in station:
                ET.SubElement(node, "tag", k="capacity", v=str(station['capacity']))

            progress.update(task, advance=1, status=station['name'])

    LOG.info(f"Found {number_of_existing_nodes} existing nodes in OpenStreetMap. Only adding tags to those nodes, no overwriting of existing data. The only exception is the 'capacity' tag, which we'll overwrite because the GBFS feed is the source of truth for this data.")
    LOG.info(f"Writing {output_file}...")
    tree = ET.ElementTree(root)
    ET.indent(tree)
    tree.write(output_file, encoding="utf-8", xml_declaration=True)

    LOG.info("Conversion complete!")


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
