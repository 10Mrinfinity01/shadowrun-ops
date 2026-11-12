import os

from dotenv import load_dotenv

load_dotenv()

# What the agent watches. "health" is reached from the host through the published ports.
SERVICES = {
    "storefront": {"container": "shop-storefront", "health": "http://localhost:8080/health"},
    "cart": {"container": "shop-cart", "health": "http://localhost:8081/health"},
    "payment": {"container": "shop-payment", "health": "http://localhost:8082/health"},
    "database": {"container": "demo-db", "health": None},
}

# Who depends on whom. Used to tell the root cause apart from its symptoms.
DEPENDS_ON = {
    "storefront": ["cart", "payment"],
    "cart": ["database"],
    "payment": ["database"],
    "database": [],
}

CUSTOMER_PROBE_URL = "http://localhost:8080/api/probe"

DOCKER_READ_URL = os.getenv("DOCKER_READ_URL", "tcp://127.0.0.1:2375")
DOCKER_EXEC_URL = os.getenv("DOCKER_EXEC_URL", "tcp://127.0.0.1:2376")
GRANT_SECRET = os.getenv("GRANT_SECRET", "")

POLL_SECONDS = 3
BAD_POLLS_BEFORE_INCIDENT = 2
QUIET_SECONDS_AFTER_INCIDENT = 15
