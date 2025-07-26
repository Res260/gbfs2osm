import importlib.metadata
import logging
import xml.etree.ElementTree as ET
from datetime import datetime

import requests
import typer
from requests import HTTPError, Response
from rich.logging import RichHandler
from rich.progress import Progress
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

version = importlib.metadata.version('gbfs2osm')


@app.command()
def convert(
    operator: Annotated[str, typer.Option("--operator", help="The human-readable name of the organization that operates the bikeshare", prompt="The human-readable name of the organization that operates the bikeshare")],
    network: Annotated[str, typer.Option("--network", help="The name of the bikeshare network. Refer to https://wiki.openstreetmap.org/wiki/Tag:amenity%3Dbicycle_rental for a list of some of them.", prompt="The name of the bikeshare network. Refer to https://wiki.openstreetmap.org/wiki/Tag:amenity%3Dbicycle_rental for a list of some of them.")],
    gbfs_feed_url: Annotated[str, typer.Option("--gbfs-feed-url", help="Link to the GBFS endpoint. Example: https://gbfs.velobixi.com/gbfs/2-2/gbfs.json",
                                               prompt="Link to the GBFS endpoint.")] = "https://gbfs.velobixi.com/gbfs/2-2/gbfs.json",
    output_file: Annotated[str, typer.Option("--output-file", help="Path to the output OSM file.", prompt="Path to the output OSM file.")] = "output.osm",
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
    gbfs_last_updated = response['last_updated']
    gbfs_last_updated_formatted = datetime.fromtimestamp(gbfs_last_updated).strftime('%Y-%m-%dT%H:%M:%SZ')
    gbfs_station_data = response['data']['stations']

    root = ET.Element("osm", version="0.6", generator=f"gbfs2osm {version}")

    with Progress() as progress:
        task = progress.add_task("Processing stations", total=len(gbfs_station_data))
        for i, station in enumerate(gbfs_station_data):
            node = ET.SubElement(root, "node",
                                 lat=str(station['lat']),
                                 lon=str(station['lon']),
                                 version="1", id=str(-i - 1))
            ET.SubElement(node, "tag", k="bicycle_rental", v="docking_station")
            ET.SubElement(node, "tag", k="amenity", v="bicycle_rental")
            ET.SubElement(node, "tag", k="name", v=station['name'])
            ET.SubElement(node, "tag", k="ref", v=f"gbfs={system_id}:{station['station_id']}")
            ET.SubElement(node, "tag", k="network", v=network)
            ET.SubElement(node, "tag", k="operator", v=operator)
            if phone_number:
                ET.SubElement(node, "tag", k="operator:phone", v=phone_number)
            if url:
                ET.SubElement(node, "tag", k="operator:website", v=url)

            if 'capacity' in station:
                ET.SubElement(node, "tag", k="capacity", v=str(station['capacity']))

            progress.update(task, advance=1)

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


app()
