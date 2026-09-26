"""Cosmos DB persistence for the tenant path (MULTIUSER_PLAN §3.1/§3.2/§10.2).

``documents`` (MU-8a) is the pure, SDK-free document mapping; ``CosmosTenantStore`` (MU-8b) will
add the async ``azure-cosmos`` client, transactional batches and IfMatch leases on top of it.
"""
