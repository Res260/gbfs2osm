
# GBFS to OSM Converter

This script converts GBFS (General Bikeshare Feed Specification) data into OpenStreetMap (OSM) format,
specifically for bicycle rental stations. It fetches the GBFS feed, processes the station data, and generates an
OSM XML file (.osm) that can be used to update OSM with new or modified bicycle rental stations.

Most importantly, it ensures to *edit* existing bicycle rental stations in OSM rather than creating new
ones when possible. It does this by using the Overpass API to fetch existing stations and match them
based on locations.

## Documentation

For full documentation, please refer to the [OSM wiki page](https://wiki.openstreetmap.org/wiki/Gbfs2osm).

## Installation

1. Install python 3.12+
2. Install poetry `python -m pip install poetry`
3. `poetry install`

## Usage

```bash
poetry run gbfs2osm --gbfs-feed-url https://gbfs.velobixi.com/gbfs/2-2/gbfs.json --output-file output.osm --operator Bixi --network Bixi --use-short-name-for-station-id 
```

You can then take the output file and open it in JOSM to see the result.

Before contributing the output of this script to OpenStreetMap, make sure to fully read:
- The [Automated Edits code of conduct](https://wiki.openstreetmap.org/wiki/Automated_Edits_code_of_conduct)
- The [Import/Guidelines](https://wiki.openstreetmap.org/wiki/Import/Guidelines)
- The [bicycle rental tag documentation](https://wiki.openstreetmap.org/wiki/Tag:amenity%3Dbicycle_rental)
- Validate the output using tools like the [JOSM validator](https://wiki.openstreetmap.org/wiki/JOSM/Validator).

To ignore cache and re-fetch the OSM data, delete the `cache` directory.