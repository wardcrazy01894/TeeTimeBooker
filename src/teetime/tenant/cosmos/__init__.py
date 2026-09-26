"""Cosmos DB persistence for the tenant path (MULTIUSER_PLAN §3.1/§3.2/§10.2).

``documents`` (MU-8a) is the pure, SDK-free document mapping; ``store.CosmosTenantStore`` (MU-8b)
adds the async ``azure-cosmos`` client, transactional batches, IfMatch leases and claim docs on
top of it.
"""
