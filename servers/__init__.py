"""PharmaPulse MCP servers.

Two FastMCP servers expose the tool logic:

- :mod:`servers.fda_intel` — openFDA shortages/recalls/NDC/alternatives
  with caching and exponential backoff (fda-intel).
- :mod:`servers.inventory_orders` — simulated pharmacy inventory, draft
  orders, and order submission with threshold/RBAC logic
  (inventory-orders).

Both import the shared structured tool envelope from :mod:`shared`.
"""
