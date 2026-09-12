#!/usr/bin/env python
"""실 VCF Operations/Aria Operations 연동 계정으로 접속해 원본 API 응답을 그대로 찍어보는
진단 도구.

app/integrations/vcf_ops_client.py 상단의 RESOURCE_KIND_*/PROP_*/TAG_PROPERTY_PREFIX
상수는 VCF Operations/Aria Operations의 버전과 관리팩 설정에 따라 실제 값이 다를 수
있다. [v3.4부터] 계층 탐색은 Datacenter->Cluster->HostSystem->VirtualMachine 순으로
CHILD 관계를 타고 내려가는 방식이므로, 이 스크립트도 그 방향(Datacenter/Cluster/
HostSystem 각각 1개씩의 CHILD 관계 원본 응답)을 그대로 찍어서 보여준다.

[v3.5부터] 일부 환경은 Folder를 relationships로 전혀 노출하지 않고, 태그도
"summary|tag|..." 접두어가 아니라 "vSphere Tag" 같은 다른 이름의 프로퍼티로 노출한다
(사용자가 실제 VCF Operations 콘솔 화면에서 확인). 이 스크립트는 이제 VM 프로퍼티 중
"parent"/"folder"/"tag"/"vcenter"가 포함된 것으로 보이는 항목을 개수 제한 없이 먼저
모아서 보여주고, 전체 프로퍼티 키 목록도 함께 출력한다 - 예전처럼 상위 40개만 보다가
관련 프로퍼티를 놓치는 일이 없도록 하기 위함이다.

이 스크립트는 이미 관리자 화면(계정 연동)에 등록된 실 연동 계정 하나를 골라
  1. 인증이 되는지,
  2. Datacenter/Cluster/HostSystem/VM 리소스 개수,
  3. Datacenter 1개의 CHILD 관계 원본 응답(Cluster를 찾을 수 있는지),
  4. Cluster 1개의 CHILD 관계 원본 응답(HostSystem을 찾을 수 있는지),
  5. HostSystem 1개의 CHILD 관계 원본 응답(VirtualMachine을 찾을 수 있는지),
  6. VM 1대의 계층/태그 관련 프로퍼티 전체 + 전체 프로퍼티 키 목록 + 값 샘플(상위 40개)
를 그대로 출력해서, 코드를 고치기 전에 실제 값을 눈으로 확인하고 상수를 맞출 수
있게 해준다 (Mock 계정은 API 호출이 없으므로 이 스크립트의 대상이 아니다).

실행:
    python scripts/inspect_integration_account.py --list
    python scripts/inspect_integration_account.py --account-id 3
    python scripts/inspect_integration_account.py --account-id 3 --max-vms 3
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal  # noqa: E402
from app.integrations.vcf_ops_client import (  # noqa: E402
    IntegrationConnectionInfo,
    VCFOpsRestClient,
    _normalize_property_name,
)
from app.models import IntegrationAccount  # noqa: E402
from app.security.crypto import decrypt_secret  # noqa: E402

# [v3.5] VM 프로퍼티 중 계층(Parent Cluster/Datacenter/Folder/Host/vCenter)/태그 관련
# 여부를 눈으로 빠르게 훑어볼 수 있도록, 정규화된 키에 이 키워드 중 하나라도 포함되면
# 강조 표시한다. vcf_ops_client.py의 실제 매칭 후보(parentdatacenter/parentcluster/
# parentfolder/spherefolder/spheretag 등)와 맞춰뒀다.
_HIGHLIGHT_FRAGMENTS = ("parent", "folder", "tag", "vcenter")


def _print_json(label: str, obj) -> None:
    print(f"\n--- {label} ---")
    print(json.dumps(obj, indent=2, ensure_ascii=False)[:4000])


def list_accounts() -> None:
    db = SessionLocal()
    try:
        accounts = db.query(IntegrationAccount).order_by(IntegrationAccount.id).all()
        if not accounts:
            print("등록된 연동 계정이 없습니다.")
            return
        for a in accounts:
            kind = "MOCK(데모)" if a.is_mock else "REAL"
            print(f"id={a.id}  [{kind}]  name={a.name!r}  base_url={a.base_url}  last_sync_status={a.last_sync_status}")
    finally:
        db.close()


def inspect(account_id: int, max_vms: int) -> None:
    db = SessionLocal()
    try:
        account = db.get(IntegrationAccount, account_id)
        if account is None:
            print(f"id={account_id} 연동 계정을 찾을 수 없습니다. --list 로 확인하세요.")
            return
        if account.is_mock:
            print(f"id={account_id}({account.name})는 데모(mock) 계정이라 실 API 호출을 하지 않습니다.")
            return

        conn = IntegrationConnectionInfo(
            base_url=account.base_url,
            username=account.username,
            password=decrypt_secret(account.password_encrypted),
            auth_source=account.auth_source,
            verify_ssl=account.verify_ssl,
        )
        client = VCFOpsRestClient(conn)
        try:
            print(f"연동 계정: {account.name} ({account.base_url})")
            token = client._authenticate()  # noqa: SLF001 - 진단 스크립트라 내부 메서드 직접 호출
            print(f"인증 성공 (토큰 길이 {len(token)}자)")

            dc_items = list(client._iter_resources("Datacenter"))  # noqa: SLF001
            cluster_items = list(client._iter_resources("ClusterComputeResource"))  # noqa: SLF001
            host_items = list(client._iter_resources("HostSystem"))  # noqa: SLF001
            vm_items = list(client._iter_resources("VirtualMachine"))  # noqa: SLF001
            print(
                f"리소스 개수 - Datacenter: {len(dc_items)}, Cluster: {len(cluster_items)}, "
                f"HostSystem: {len(host_items)}, VM: {len(vm_items)}"
            )

            # ---- Datacenter -> CHILD (Cluster를 찾을 수 있는지) ----
            if dc_items:
                dc = dc_items[0]
                print(f"\n{'=' * 70}\nDatacenter 샘플: {dc.get('resourceKey', {}).get('name')} (id={dc['identifier']})")
                raw = client._get(  # noqa: SLF001
                    f"/suite-api/api/resources/{dc['identifier']}/relationships",
                    params={"relationshipType": "CHILD"},
                ).json()
                _print_json("Datacenter CHILD relationships 원본 응답 (최상위 키 + Cluster 존재 여부 확인용)", raw)
                children = client._extract_relationship_list(raw)  # noqa: SLF001
                kinds = sorted({c.get("resourceKey", {}).get("resourceKindKey") for c in children})
                print(f"-> 파싱된 CHILD 종류: {kinds}")
            else:
                print("Datacenter 리소스를 하나도 찾지 못했습니다 - resourceKind='Datacenter' 자체가 이 환경과 다를 수 있습니다.")

            # ---- Cluster -> CHILD (HostSystem을 찾을 수 있는지) ----
            if cluster_items:
                cl = cluster_items[0]
                print(f"\n{'=' * 70}\nCluster 샘플: {cl.get('resourceKey', {}).get('name')} (id={cl['identifier']})")
                raw = client._get(  # noqa: SLF001
                    f"/suite-api/api/resources/{cl['identifier']}/relationships",
                    params={"relationshipType": "CHILD"},
                ).json()
                _print_json("Cluster CHILD relationships 원본 응답 (최상위 키 + HostSystem 존재 여부 확인용)", raw)
                children = client._extract_relationship_list(raw)  # noqa: SLF001
                kinds = sorted({c.get("resourceKey", {}).get("resourceKindKey") for c in children})
                print(f"-> 파싱된 CHILD 종류: {kinds}")
            else:
                print("Cluster(ClusterComputeResource) 리소스를 하나도 찾지 못했습니다.")

            # ---- HostSystem -> CHILD (VirtualMachine을 찾을 수 있는지) ----
            if host_items:
                h = host_items[0]
                print(f"\n{'=' * 70}\nHostSystem 샘플: {h.get('resourceKey', {}).get('name')} (id={h['identifier']})")
                raw = client._get(  # noqa: SLF001
                    f"/suite-api/api/resources/{h['identifier']}/relationships",
                    params={"relationshipType": "CHILD"},
                ).json()
                _print_json("HostSystem CHILD relationships 원본 응답 (최상위 키 + VirtualMachine 존재 여부 확인용)", raw)
                children = client._extract_relationship_list(raw)  # noqa: SLF001
                kinds = sorted({c.get("resourceKey", {}).get("resourceKindKey") for c in children})
                print(f"-> 파싱된 CHILD 종류: {kinds}")
            else:
                print("HostSystem 리소스를 하나도 찾지 못했습니다.")

            # ---- VM 프로퍼티 샘플 ----
            for item in vm_items[:max_vms]:
                vm_id = item["identifier"]
                vm_name = item.get("resourceKey", {}).get("name", vm_id)
                print(f"\n{'=' * 70}\nVM: {vm_name} (id={vm_id})")
                props = client._fetch_properties(vm_id)  # noqa: SLF001

                # [v3.5] "Parent Cluster"/"Parent Datacenter"/"Parent Folder"/"vSphere
                # Folder"/"vSphere Tag" 같은 계층/태그 관련 프로퍼티는 VM마다 수백 개인
                # 전체 프로퍼티 중 어디 있을지 몰라, 상위 N개로 잘라서 보여주면 놓치기
                # 쉽다. 정규화된 키에 _HIGHLIGHT_FRAGMENTS가 포함되는 프로퍼티는 개수와
                # 무관하게 항상 전부 먼저 보여준다.
                highlighted = {
                    k: v for k, v in props.items() if any(f in _normalize_property_name(k) for f in _HIGHLIGHT_FRAGMENTS)
                }
                if highlighted:
                    _print_json(
                        f"계층/태그 관련으로 보이는 프로퍼티 전체 (총 {len(highlighted)}개, "
                        "vcf_ops_client.py의 _PARENT_*/_VSPHERE_TAG_NAME_FRAGMENT 매칭 대상)",
                        highlighted,
                    )
                else:
                    print(
                        "\n[!] 계층/태그 관련(parent/folder/tag/vcenter 포함) 프로퍼티를 하나도 "
                        "찾지 못했습니다 - 아래 전체 프로퍼티 개수 목록에서 실제 키 이름을 확인하세요."
                    )
                print(f"\n전체 프로퍼티 개수: {len(props)}개")
                _print_json("전체 프로퍼티 키 목록 (값은 생략, 이름만)", sorted(props.keys()))
                _print_json("프로퍼티 값 (상위 40개, PROP_*/TAG_PROPERTY_PREFIX 값 확인용)", dict(list(props.items())[:40]))
        finally:
            client.close()
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="실 연동 계정 원본 API 응답 진단 도구")
    parser.add_argument("--list", action="store_true", help="등록된 연동 계정 목록만 출력")
    parser.add_argument("--account-id", type=int, help="진단할 연동 계정 id")
    parser.add_argument("--max-vms", type=int, default=1, help="상세 조회할 VM 개수 (기본 1대)")
    args = parser.parse_args()

    if args.list or args.account_id is None:
        list_accounts()
    else:
        inspect(args.account_id, args.max_vms)
