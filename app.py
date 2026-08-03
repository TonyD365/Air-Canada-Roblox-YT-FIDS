"""Process entry point.

    python app.py             # start the broadcast (or dry-run without RTMP_URL)
    python app.py --preview   # write a single PNG frame and exit

This file contains no business logic: it reads the environment, builds the
object graph and hands control to :class:`crane_fids.application.Application`.

External code can import this module and update flights at runtime::

    import app
    from crane_fids import Flight, FlightStatus
    from datetime import datetime

    app.provider.set_flights([
        Flight("CR999", "DUBAI", datetime(2025, 7, 17, 14, 30), "F01", FlightStatus.ON_TIME),
    ])
"""

from __future__ import annotations
#from dotenv import load_dotenv


import argparse
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
from flask import Flask, request, jsonify

import crane_fids
from crane_fids import __version__, BoardKind, Flight, FlightStatus
from crane_fids.application import build_application
from crane_fids.config import Config, ConfigError
from crane_fids.logging_setup import configure_logging
from crane_fids.renderer import FidsRenderer, FrameContext
from datetime import datetime


_LOG = logging.getLogger("crane_fids.app")

#load_dotenv()

# ------------------------------------------------------------------ #
# Flask HTTP server
# ------------------------------------------------------------------ #
flask_app = Flask(__name__)


@flask_app.route("/", methods=["GET", "HEAD"])
def index() -> tuple[str, int]:
    """Health check and status page."""
    html = """<!DOCTYPE html>
<html>
<head>
    <title>Crane Airlines FIDS</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; background: #f0f0f0; }}
        .container {{ background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); margin-bottom: 20px; }}
        h1 {{ color: #333; }}
        .status {{ color: #28a745; font-weight: bold; }}
        .info {{ margin: 20px 0; }}
        .footer {{ margin-top: 30px; font-size: 12px; color: #666; }}
        
        .management-section {{ background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); margin-bottom: 20px; }}
        .form-group {{ margin-bottom: 15px; }}
        label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
        input, select {{ width: 100%; padding: 8px; box-sizing: border-box; border: 1px solid #ddd; border-radius: 4px; }}
        button {{ background: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; margin-right: 10px; }}
        button:hover {{ background: #0056b3; }}
        button.delete {{ background: #dc3545; }}
        button.delete:hover {{ background: #c82333; }}
        
        .flight-list {{ background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        .flight-item {{ padding: 10px; margin: 10px 0; background: #f8f9fa; border-left: 4px solid #007bff; border-radius: 4px; }}
        .flight-item.delete-btn {{ border-left-color: #dc3545; }}
        
        .tabs {{ display: flex; margin-bottom: 20px; border-bottom: 2px solid #ddd; }}
        .tab {{ padding: 10px 20px; cursor: pointer; background: #f8f9fa; border: none; border-radius: 4px 4px 0 0; }}
        .tab.active {{ background: #007bff; color: white; }}
        .tab-content {{ display: none; }}
        .tab-content.active {{ display: block; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🛫 Crane Airlines FIDS System</h1>
        <p class="status">✓ System is running</p>
        <div class="info">
            <p><strong>Version:</strong> {version}</p>
            <p><strong>Status:</strong> Active</p>
            <p><strong>Port:</strong> 7860</p>
        </div>
    </div>
    
    <div class="management-section">
        <h2>✈️ Flight Management</h2>
        
        <div class="tabs">
            <button type="button" class="tab active" onclick="showTab('add')">Add Flight</button>
            <button type="button" class="tab" onclick="showTab('update')">Update Flight</button>
            <button type="button" class="tab" onclick="showTab('delete')">Delete Flight</button>
        </div>
        
        <div id="add" class="tab-content active">
            <h3>Add New Flight</h3>
            <form onsubmit="addFlight(event)">
                <div class="form-group">
                    <label>Flight Number:</label>
                    <input type="text" id="add_flight_number" required placeholder="e.g., CR101">
                </div>
                <div class="form-group">
                    <label>Destination:</label>
                    <input type="text" id="add_destination" required placeholder="e.g., NEW YORK">
                </div>
                <div class="form-group">
                    <label>Scheduled Time (HH:MM):</label>
                    <input type="time" id="add_scheduled" required>
                </div>
                <div class="form-group">
                    <label>Gate:</label>
                    <input type="text" id="add_gate" required placeholder="e.g., A12">
                </div>
                <div class="form-group">
                    <label>Status:</label>
                    <select id="add_status">
                        <option value="ON_TIME">ON TIME</option>
                        <option value="GATE_OPEN">GATE OPEN</option>
                        <option value="EC_BOARDING">ECONOMY CLASS BOARDING</option>
                        <option value="BC_BOARDING">BC BOARDING</option>
                        <option value="FC_BOARDING">FC BOARDING</option>
                        <option value="FINAL_CALL">FINAL CALL</option>
                        <option value="DELAYED">DELAYED</option>
                        <option value="CANCELLED">CANCELLED</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Departure (optional):</label>
                    <input type="text" id="add_departure" placeholder="e.g., TORONTO">
                </div>
                <button type="submit">Add Flight</button>
            </form>
        </div>
        
        <div id="update" class="tab-content">
            <h3>Update Flight</h3>
            <form onsubmit="updateFlight(event)">
                <div class="form-group">
                    <label>Flight Number:</label>
                    <input type="text" id="update_flight_number" required placeholder="e.g., CR101">
                </div>
                <div class="form-group">
                    <label>New Status:</label>
                    <select id="update_status">
                        <option value="ON_TIME">ON TIME</option>
                        <option value="GATE_OPEN">GATE OPEN</option>
                        <option value="EC_BOARDING">ECONOMY CLASS BOARDING</option>
                        <option value="BC_BOARDING">BC BOARDING</option>
                        <option value="FC_BOARDING">FC BOARDING</option>
                        <option value="FINAL_CALL">FINAL CALL</option>
                        <option value="DELAYED">DELAYED</option>
                        <option value="CANCELLED">CANCELLED</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>New Gate (optional):</label>
                    <input type="text" id="update_gate" placeholder="e.g., A12">
                </div>
                <button type="submit">Update Flight</button>
            </form>
        </div>
        
        <div id="delete" class="tab-content">
            <h3>Delete Flight</h3>
            <form onsubmit="deleteFlight(event)">
                <div class="form-group">
                    <label>Flight Number:</label>
                    <input type="text" id="delete_flight_number" required placeholder="e.g., CR101">
                </div>
                <button type="submit" class="delete">Delete Flight</button>
            </form>
        </div>
    </div>
    
    <div class="flight-list">
        <h2>📋 Current Flights</h2>
        <div id="flights-container">
            <p>Loading flights...</p>
        </div>
    </div>

        <script>
        function showTab(tabName) {{
            document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));
            document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
            document.getElementById(tabName).classList.add('active');
            // Find the clicked tab button and add active class
            const tabs = document.querySelectorAll('.tab');
            tabs.forEach(tab => {{
                if (tab.getAttribute('onclick').includes(tabName)) {{
                    tab.classList.add('active');
                }}
            }});
        }}
        
        async function addFlight(event) {{
            event.preventDefault();
            const flight_number = document.getElementById('add_flight_number').value;
            const destination = document.getElementById('add_destination').value;
            const scheduled = document.getElementById('add_scheduled').value;
            const gate = document.getElementById('add_gate').value;
            const status = document.getElementById('add_status').value;
            const departure = document.getElementById('add_departure').value;
            
            const response = await fetch('/api/flights', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{"flight_number": flight_number, "destination": destination, "scheduled": scheduled, "gate": gate, "status": status, "departure": departure}})
            }});
            
            if (response.ok) {{
                alert('Flight added successfully!');
                location.reload();
            }} else {{
                alert('Error adding flight');
            }}
        }}
        
        async function updateFlight(event) {{
            event.preventDefault();
            const flight_number = document.getElementById('update_flight_number').value;
            const status = document.getElementById('update_status').value;
            const gate = document.getElementById('update_gate').value;
            
            const response = await fetch('/api/flights/' + flight_number, {{
                method: 'PUT',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{"status": status, "gate": gate}})
            }});
            
            if (response.ok) {{
                alert('Flight updated successfully!');
                location.reload();
            }} else {{
                alert('Error updating flight');
            }}
        }}
        
        async function deleteFlight(event) {{
            event.preventDefault();
            const flight_number = document.getElementById('delete_flight_number').value;
            
            if (!confirm(`Are you sure you want to delete flight ${{flight_number}}?`)) {{
                return;
            }}
            
            const response = await fetch('/api/flights/' + flight_number, {{
                method: 'DELETE'
            }});
            
            if (response.ok) {{
                alert('Flight deleted successfully!');
                location.reload();
            }} else {{
                alert('Error deleting flight');
            }}
        }}
        
        async function loadFlights() {{
            const response = await fetch('/api/flights');
            const flights = await response.json();
            const container = document.getElementById('flights-container');
            
            if (flights.length === 0) {{
                container.innerHTML = '<p>No flights scheduled</p>';
                return;
            }}
            
            container.innerHTML = flights.map(f => `
                <div class="flight-item">
                    <strong>${{f.flight_number}}</strong> - ${{f.destination}} | 
                    Time: ${{f.scheduled}} | Gate: ${{f.gate}} | 
                    Status: ${{f.status}} | Departure: ${{f.departure || 'N/A'}}
                </div>
            `).join('');
        }}
        
        loadFlights();
    </script>
</body>
</html>""".format(version=__version__)
    return html, 200


@flask_app.route("/api/flights", methods=["GET"])
def get_flights() -> tuple[str, int]:
    """Get all current flights."""
    now = datetime.now(timezone.utc)
    flights = provider.fetch(now, BoardKind.DEPARTURES)
    flights_list = []
    for flight in flights:
        flights_list.append({
            "flight_number": flight.flight_number,
            "destination": flight.destination,
            "scheduled": flight.scheduled.strftime("%Y-%m-%d %H:%M:%S"),
            "gate": flight.gate,
            "status": flight.status.name,
            "departure": flight.departure
        })
    return jsonify(flights_list), 200


@flask_app.route("/api/flights", methods=["POST"])
def add_flight() -> tuple[str, int]:
    """Add a new flight."""
    try:
        data = request.get_json()
        flight_number = data.get("flight_number")
        destination = data.get("destination")
        scheduled_str = data.get("scheduled")
        gate = data.get("gate")
        status_str = data.get("status", "ON_TIME")
        departure = data.get("departure", "")
        
        # Parse scheduled time
        scheduled = datetime.strptime(scheduled_str, "%H:%M").replace(
            year=datetime.now().year,
            month=datetime.now().month,
            day=datetime.now().day,
            tzinfo=timezone.utc
        )
        
        # Get status enum
        status = FlightStatus[status_str]
        
        # Create new flight
        new_flight = Flight(
            flight_number=flight_number,
            destination=destination,
            scheduled=scheduled,
            gate=gate,
            status=status,
            departure=departure
        )
        
        # Add to provider
        current_flights = list(provider.fetch(datetime.now(timezone.utc), BoardKind.DEPARTURES))
        current_flights.append(new_flight)
        provider.set_flights(current_flights)
        
        return jsonify({"message": "Flight added successfully"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@flask_app.route("/api/flights/<flight_number>", methods=["PUT"])
def update_flight(flight_number: str) -> tuple[str, int]:
    """Update an existing flight."""
    try:
        data = request.get_json()
        status_str = data.get("status")
        gate = data.get("gate", "")
        
        # Get current flights
        now = datetime.now(timezone.utc)
        current_flights = list(provider.fetch(now, BoardKind.DEPARTURES))
        
        # Find and update the flight
        updated_flights = []
        found = False
        for flight in current_flights:
            if flight.flight_number == flight_number:
                # Create updated flight with new status/gate
                updated_flight = flight.with_status(
                    FlightStatus[status_str] if status_str else flight.status,
                    remark=flight.remark
                )
                # Note: gate is immutable in the current model, so we need to create a new Flight
                if gate:
                    updated_flight = Flight(
                        flight_number=updated_flight.flight_number,
                        destination=updated_flight.destination,
                        scheduled=updated_flight.scheduled,
                        gate=gate,
                        status=updated_flight.status,
                        departure=updated_flight.departure,
                        remark=updated_flight.remark,
                        board=updated_flight.board
                    )
                updated_flights.append(updated_flight)
                found = True
            else:
                updated_flights.append(flight)
        
        if not found:
            return jsonify({"error": "Flight not found"}), 404
        
        provider.set_flights(updated_flights)
        return jsonify({"message": "Flight updated successfully"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@flask_app.route("/api/flights/<flight_number>", methods=["DELETE"])
def delete_flight(flight_number: str) -> tuple[str, int]:
    """Delete a flight."""
    try:
        now = datetime.now(timezone.utc)
        current_flights = list(provider.fetch(now, BoardKind.DEPARTURES))
        
        # Filter out the flight to delete
        updated_flights = [f for f in current_flights if f.flight_number != flight_number]
        
        if len(updated_flights) == len(current_flights):
            return jsonify({"error": "Flight not found"}), 404
        
        provider.set_flights(updated_flights)
        return jsonify({"message": "Flight deleted successfully"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400


def _start_flask_server(port: int = 7860) -> None:
    """Start Flask server in a background thread."""
    _LOG.info("Flask server starting on port %d", port)
    try:
        flask_app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
    except Exception as exc:
        _LOG.error("Flask server error: %s", exc)

# ------------------------------------------------------------------ #
# Default example flights shown when the board starts
# ------------------------------------------------------------------ #
def _default_flights() -> list[Flight]:
    """Return a handful of example flights for the current time."""
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    base = now + timedelta(minutes=5)  # first flight 5 minutes from now
    """
        Flight("CR101", "NEW YORK",     base,                          "A12", FlightStatus.ON_TIME,     departure="NEWARK"),
        Flight("CR102", "CHICAGO",      base + timedelta(minutes=15),  "B08", FlightStatus.ON_TIME,     departure="CHICAGO"),
        Flight("CR103", "TORONTO",      base + timedelta(minutes=30),  "C11", FlightStatus.GATE_OPEN,   departure="TORONTO"),
        Flight("CR201", "VANCOUVER",    base + timedelta(minutes=45),  "D09", FlightStatus.BOARDING,    departure="VANCOUVER"),
        Flight("CR301", "LONDON",       base + timedelta(minutes=60),  "E73", FlightStatus.ON_TIME,     departure="LONDON"),
        Flight("CR401", "PARIS",        base + timedelta(minutes=75),  "A12", FlightStatus.LAST_CALL,   departure="PARIS"),
        Flight("CR118", "FRANKFURT",    base + timedelta(minutes=90),  "B08", FlightStatus.DELAYED,     departure="FRANKFURT",
               remark="NEW TIME " + (base + timedelta(minutes=105)).strftime("%H:%M")),
        Flight("CR233", "TOKYO",        base + timedelta(minutes=105), "C11", FlightStatus.ON_TIME,     departure="TOKYO"),
        Flight("CR507", "SHANGHAI",     base + timedelta(minutes=120), "D09", FlightStatus.FINAL_CALL,  departure="SHANGHAI"),
        Flight("CR612", "BEIJING",      base + timedelta(minutes=135), "E73", FlightStatus.ON_TIME,     departure="BEIJING"),
        Flight("CR715", "SINGAPORE",    base + timedelta(minutes=150), "A12", FlightStatus.ON_TIME,     departure="SINGAPORE"),
        Flight("CR808", "SYDNEY",       base + timedelta(minutes=165), "B08", FlightStatus.CANCELLED,   departure="SYDNEY"),
    """
    toronto_time_17_00 = datetime(2025, 7, 19, 21, 0, tzinfo=timezone.utc)

    return [
        #Flight("CR015", "[YYZ] Toronto Pearson International Airport, Canada",     toronto_time_17_00,                          "5", FlightStatus.DEPARTED,     departure="[PEK] Beijing Capital International Airport, China"),
    ]


# ------------------------------------------------------------------ #
# Use the global provider from crane_fids module
# ------------------------------------------------------------------ #
provider = crane_fids._provider
crane_fids.set_flights(_default_flights())


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="crane-fids",
        description="Crane Airlines airport FIDS renderer and RTMP broadcaster.",
    )
    parser.add_argument("--version", action="version", version=f"crane-fids {__version__}")
    parser.add_argument(
        "--preview",
        metavar="PATH",
        nargs="?",
        const="preview.png",
        help="Render one frame to PATH (default: preview.png) and exit.",
    )
    parser.add_argument(
        "--preview-seconds",
        type=float,
        default=0.0,
        help="Stream time of the previewed frame, in seconds (affects blink/scroll phase).",
    )
    return parser.parse_args(argv)


def _run_preview(config: Config, path: Path, seconds: float) -> int:
    """Render a single frame to disk: the fastest way to review a layout."""
    now = datetime.now(timezone.utc)
    flights = tuple(provider.fetch(now, BoardKind.DEPARTURES))
    renderer = FidsRenderer.from_config(config)
    ctx = FrameContext(
        now=now,
        frame_index=int(seconds * config.video.fps),
        fps=config.video.fps,
        flights=flights,
        airline_name=config.airline_name,
        airport_name=config.airport_name,
        board_title=config.board_title,
        ticker_text=config.ticker_text,
        timezone_label=config.timezone_label,
        rows_per_page=config.rows_per_page,
        page_seconds=config.page_seconds,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    renderer.render(ctx).save(path)
    _LOG.info("Preview written to %s (%d flights)", path, len(flights))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Program entry point.  Returns a POSIX exit code."""
    args = _parse_args(argv)
    try:
        config = Config.from_env()
    except ConfigError as exc:
        configure_logging("INFO")
        _LOG.error("Invalid configuration: %s", exc)
        return 2

    configure_logging(config.log_level)
    _LOG.info("crane-fids %s", __version__)

    if args.preview:
        return _run_preview(config, Path(args.preview), args.preview_seconds)

    # Start Flask server in background thread
    server_thread = threading.Thread(target=_start_flask_server, args=(7860,), daemon=True)
    server_thread.start()
    _LOG.info("Flask server thread started on port 7860")

    return build_application(config, provider=provider).run()


if __name__ == "__main__":
    main()