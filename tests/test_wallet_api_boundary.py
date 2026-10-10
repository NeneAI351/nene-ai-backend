"""Regression tests for the public wallet API boundary."""
from wallet import router


def test_wallet_mutations_are_not_exposed_as_http_routes():
    """Clients must not choose reserve/capture/release amounts or settle their own jobs."""
    exposed = {
        (route.path, method)
        for route in router.routes
        for method in (getattr(route, "methods", None) or set())
    }

    assert ("/api/wallet", "GET") in exposed
    assert ("/api/wallet/ledger", "GET") in exposed
    for path in (
        "/api/wallet/grant",
        "/api/wallet/reserve",
        "/api/wallet/release",
        "/api/wallet/capture",
    ):
        assert not any(route_path == path for route_path, _ in exposed), (
            f"{path} must not be exposed as a public HTTP route"
        )


def test_wallet_read_routes_require_verified_auth_dependency():
    for route in router.routes:
        if route.path in {"/api/wallet", "/api/wallet/ledger"}:
            dependency_callables = {
                dependency.call for dependency in route.dependant.dependencies
            }
            from auth import get_authenticated_user_id

            assert get_authenticated_user_id in dependency_callables
