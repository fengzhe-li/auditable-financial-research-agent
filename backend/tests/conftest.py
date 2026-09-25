from __future__ import annotations

import pytest

from afra.orchestrator.orchestrator import ResearchTaskOrchestrator
from afra.providers.test_double import (
    EnterpriseApprovedTestDoubleProvider,
    PrivateLocalTestDoubleProvider,
    TestDoubleProvider,
)
from afra.storage.repository import Repository


@pytest.fixture
def db_path(tmp_path):
    """A real on-disk SQLite path (not :memory:) - required for the
    resumability test to mean anything, and used everywhere else too so
    every test exercises the same real persistence path.
    """
    return tmp_path / "afra_test.db"


@pytest.fixture
def repository(db_path):
    repo = Repository(db_path)
    yield repo
    repo.close()


@pytest.fixture
def provider():
    return TestDoubleProvider()


@pytest.fixture
def enterprise_provider():
    return EnterpriseApprovedTestDoubleProvider()


@pytest.fixture
def private_provider():
    return PrivateLocalTestDoubleProvider()


@pytest.fixture
def providers(provider, enterprise_provider, private_provider):
    """The full Phase 4 provider registry - all three provider classes
    registered, matching a realistic deployment where more than one
    provider is available for afra.policy.enforcement to route between.
    """
    return {
        provider.provider_class: provider,
        enterprise_provider.provider_class: enterprise_provider,
        private_provider.provider_class: private_provider,
    }


@pytest.fixture
def orchestrator(repository, provider, providers):
    return ResearchTaskOrchestrator(repository, provider, providers=providers)
