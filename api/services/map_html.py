"""Step 5: the trip as an HTML map (Leaflet): route line, candidate stations, stops."""
from django.template.loader import render_to_string

from api.services.trip import summary_of


def map_data_of(trip):
    """Everything the map page draws, as plain JSON. Saved with the trip, to show it later."""
    plan = trip.plan
    candidates = []
    for candidate in plan.candidates:
        row = trip.stations.rows[candidate.station]
        candidates.append({
            'lat': row['lat'], 'lng': row['lon'], 'name': row['name'],
            'price': round(candidate.price, 3), 'mile': round(candidate.mile),
            'precision': row['precision'],
        })
    stops = [{key: value for key, value in stop.items() if key != 'station_pk'}
             for stop in trip.stops]
    return {
        'line': [[lat, lng] for lng, lat in plan.route.line()],
        'candidates': candidates,
        'stops': stops,
        'start': {'lat': trip.start.lat, 'lng': trip.start.lng, 'label': trip.start.label},
        'finish': {'lat': trip.finish.lat, 'lng': trip.finish.lng, 'label': trip.finish.label},
        'summary': summary_of(trip),
        'warnings': trip.warnings,
    }


def render(map_data):
    """Saved map data -> HTML page. Names are escaped in the browser, never put into markup here."""
    return render_to_string('api/route_map.html', {'data': map_data})
