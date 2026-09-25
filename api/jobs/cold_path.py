"""Prozess des kalten Pfads: Dispatcher Richtung n8n plus Waechter fuer den Kuechenbon.

`python -m api.jobs.cold_path` (docker-compose, Dienst `dispatcher`). Der Waechter
gehoert zur Fachlogik (domain/ordering/handover.py), der Dispatcher zu events/;
hier werden beide zusammengesteckt, damit events/ die Fachlogik nicht kennen muss.
"""

from api.config import settings
from api.core.logging import configure_logging
from api.domain.ordering.handover import sweep
from api.events.dispatcher import run_forever, send_to_n8n

if __name__ == "__main__":
    configure_logging(settings.log_level)
    run_forever(send_to_n8n, tick=sweep)
