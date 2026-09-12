"""VCF Operations/Aria Operations 연동 어댑터 패키지."""
from __future__ import annotations

from app.integrations.base import TagRef, VCFOpsClient, VMSnapshot
from app.integrations.mock_client import build_mock_client
from app.models import IntegrationAccount


def build_client_for_integration_account(account: IntegrationAccount) -> VCFOpsClient:
    """독립 등록된 연동 계정(IntegrationAccount)에 대응하는 클라이언트를 반환한다.

    is_mock=True (seed_data.py가 생성하는 데모 계정)이면 샘플 데이터 클라이언트를,
    그 외에는 실제 VCF Operations/Aria Operations REST API 클라이언트를 반환한다.
    """
    if account.is_mock:
        return build_mock_client()

    # 지연 import: httpx 등 실 연동 의존성은 실제 사용 시점에만 로드
    from app.integrations.vcf_ops_client import IntegrationConnectionInfo, VCFOpsRestClient
    from app.security.crypto import decrypt_secret

    conn = IntegrationConnectionInfo(
        base_url=account.base_url,
        username=account.username,
        password=decrypt_secret(account.password_encrypted),
        auth_source=account.auth_source,
        verify_ssl=account.verify_ssl,
    )
    return VCFOpsRestClient(conn)


__all__ = ["TagRef", "VCFOpsClient", "VMSnapshot", "build_client_for_integration_account"]
