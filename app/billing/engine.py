"""
Billing 계산 엔진 (순수 함수, DB에 의존하지 않음).

과금 원칙
---------
- 과금 단위 시간 = 수집 간격(block_minutes) 블록 (PowerSample 1행 = 1블록 = collector 1회
  수집). 기본값은 5분(app/config.py의 collector_interval_minutes)이며, 호출자가
  명시적으로 넘기지 않으면 이 기본값을 사용한다.
- VM이 해당 블록 동안 Power-On 상태로 "관측"된 경우에만 과금 대상
  (즉 실제 과금 시간은 항상 block_minutes의 배수로 절상/집계됨)
- 블록 비용 =
      vCPU수        * vcpu_rate_per_hour  * (block_minutes/60) * cpu_usage_weight
    + vMEM(GB)      * vmem_rate_per_hour_gb * (block_minutes/60) * mem_usage_weight
    + vDisk(GB)     * vdisk_rate_per_hour_gb * (block_minutes/60)
  vDisk는 사용량 가중치 대상이 아니다(할당된 스토리지는 순간 IO 사용량과 무관하게 항상
  점유하고 있다고 보는 것이 일반적인 클라우드 과금 관례).
- 기간 합계 = 기간 내 모든 Power-On 블록 비용의 합

[v3.7] 사용량 가중치 하이브리드 과금
------------------------------------
RateCard.usage_weight_enabled가 꺼져 있으면(기본값) cpu_usage_weight/mem_usage_weight는
항상 1.0이라 위 식은 기존(v3~v3.6)과 완전히 동일하게 동작한다 - 순수 스펙 기준 정액
과금이라는 기존 계약을 깨지 않기 위해 기본값을 꺼둔 채로 도입했다.

켜면, VM의 그 블록 시점 실사용률(cpu_usage_pct/mem_usage_pct, 0~100%, VCF Operations의
cpu|usage_average/mem|usage_average에서 옴 - app/integrations/vcf_ops_client.py 참고)을
0.0~1.0 비율로 환산해 가중치를 계산한다:

    weight = usage_weight_floor + (1 - usage_weight_floor) * usage_ratio

usage_weight_floor(RateCard.usage_weight_floor_pct/100, 기본 0.3)는 "완전 유휴(0% 사용)"
여도 최소 이 비율만큼은 과금하는 바닥값이다 - 정액 요금의 성격을 일부 남겨, "가중치를
켰더니 안 쓰는 VM은 0원이 되어버리는" 극단적 결과를 방지한다(플로어=1.0이면 사실상
가중치가 꺼진 것과 같고, 플로어=0.0이면 완전 종량제가 된다). usage_pct가 None(수집
실패, 미지원 환경, 아직 채워지지 않은 과거 데이터 등)이면 그 블록은 weight=1.0(가중치
없음 = 정액 그대로)으로 처리한다 - 즉 사용량 데이터가 없다고 과금이 실패하거나 VM이
과소 과금되지 않는다.

데모 단순화 안내
----------------
현재 구현은 "조회 시점의 RateCard(현재 단가/가중치 설정)"를 조회 기간 전체에 일괄
적용합니다. 실 운영에서는 단가 변경 이력(RateCardHistory)의 effective 기간을 반영하여,
변경 시점 이전/이후 블록에 각각 다른 단가/가중치 설정을 적용하는 것이 정확한 과금입니다.

마찬가지로, 수집 간격(block_minutes)을 운영 중 변경하면 과거에 다른 간격으로 적재된
PowerSample도 새 간격 기준으로 일괄 환산되어 집계됩니다 (블록별로 실제 적용되었던
간격을 개별 기록하지 않음) — 위 RateCard 소급 적용과 동일한 성격의 단순화입니다.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import get_settings

DEFAULT_BLOCK_MINUTES = get_settings().collector_interval_minutes


@dataclass
class RateInput:
    vcpu_rate_per_hour: float
    vmem_rate_per_hour_gb: float
    vdisk_rate_per_hour_gb: float
    currency: str = "KRW"
    # [v3.7] 기본값(꺼짐/0.3)은 "가중치 미사용"과 동일하게 동작해 기존 호출자(및 과거
    # RateCard가 없는 프로젝트)에도 안전하다.
    usage_weight_enabled: bool = False
    usage_weight_floor: float = 0.3  # 0.0~1.0 비율 (RateCard.usage_weight_floor_pct/100)


@dataclass
class VmUsageAccumulator:
    """VM 1대에 대한 기간 집계 결과 (누적 계산용)."""

    vm_id: int
    vm_name: str
    vcpu_count: int
    vmem_gb: float
    vdisk_gb: float
    block_minutes: int = DEFAULT_BLOCK_MINUTES
    powered_on_blocks: int = 0
    total_blocks: int = 0

    vcpu_cost: float = 0.0
    vmem_cost: float = 0.0
    vdisk_cost: float = 0.0

    # [v3.7] 평균 사용률 표시용 누적(요금 계산 자체와는 별개 - 화면에 "평균 CPU/MEM
    # 사용률"을 보여주기 위함). 값이 있는 블록만 누적하므로, 수집 실패로 일부 블록에
    # None이 섞여 있어도 평균이 왜곡되지 않는다.
    cpu_usage_sum: float = 0.0
    cpu_usage_samples: int = 0
    mem_usage_sum: float = 0.0
    mem_usage_samples: int = 0

    @property
    def block_hours(self) -> float:
        return self.block_minutes / 60.0

    @property
    def powered_on_hours(self) -> float:
        return round(self.powered_on_blocks * self.block_hours, 2)

    @property
    def total_cost(self) -> float:
        return round(self.vcpu_cost + self.vmem_cost + self.vdisk_cost, 2)

    @property
    def uptime_ratio(self) -> float:
        return round(self.powered_on_blocks / self.total_blocks, 4) if self.total_blocks else 0.0

    @property
    def avg_cpu_usage_pct(self) -> float | None:
        return round(self.cpu_usage_sum / self.cpu_usage_samples, 1) if self.cpu_usage_samples else None

    @property
    def avg_mem_usage_pct(self) -> float | None:
        return round(self.mem_usage_sum / self.mem_usage_samples, 1) if self.mem_usage_samples else None


def _usage_weight(usage_pct: float | None, rate: RateInput) -> float:
    """usage_pct(0~100 또는 None)를 이 RateInput 설정에 따른 가중치(0~1 스케일은 아님,
    floor~1.0 범위)로 변환한다. 가중치가 꺼져 있거나 값이 없으면 1.0(기존과 동일)."""
    if not rate.usage_weight_enabled or usage_pct is None:
        return 1.0
    ratio = max(0.0, min(1.0, usage_pct / 100.0))
    floor = max(0.0, min(1.0, rate.usage_weight_floor))
    return floor + (1.0 - floor) * ratio


def cost_of_block(
    vcpu_count: int,
    vmem_gb: float,
    vdisk_gb: float,
    rate: RateInput,
    block_minutes: int = DEFAULT_BLOCK_MINUTES,
    cpu_usage_pct: float | None = None,
    mem_usage_pct: float | None = None,
) -> tuple[float, float, float]:
    """block_minutes 분 블록 1개에 대한 (vcpu_cost, vmem_cost, vdisk_cost) 를 반환한다.

    cpu_usage_pct/mem_usage_pct는 [v3.7] 사용량 가중치 하이브리드 과금 입력값 - 생략하면
    (기존 호출부 호환) 가중치 없이 기존과 동일하게 계산된다.
    """
    block_hours = block_minutes / 60.0
    cpu_weight = _usage_weight(cpu_usage_pct, rate)
    mem_weight = _usage_weight(mem_usage_pct, rate)
    vcpu_cost = vcpu_count * rate.vcpu_rate_per_hour * block_hours * cpu_weight
    vmem_cost = vmem_gb * rate.vmem_rate_per_hour_gb * block_hours * mem_weight
    vdisk_cost = vdisk_gb * rate.vdisk_rate_per_hour_gb * block_hours
    return vcpu_cost, vmem_cost, vdisk_cost


def accumulate_sample(
    acc: VmUsageAccumulator,
    power_state: str,
    vcpu_count: int,
    vmem_gb: float,
    vdisk_gb: float,
    rate: RateInput,
    cpu_usage_pct: float | None = None,
    mem_usage_pct: float | None = None,
) -> None:
    """PowerSample 1건을 누적기에 반영한다. Power-Off 블록은 과금하지 않는다."""
    acc.total_blocks += 1
    if power_state != "on":
        return
    acc.powered_on_blocks += 1
    vcpu_cost, vmem_cost, vdisk_cost = cost_of_block(
        vcpu_count, vmem_gb, vdisk_gb, rate, acc.block_minutes, cpu_usage_pct, mem_usage_pct
    )
    acc.vcpu_cost += vcpu_cost
    acc.vmem_cost += vmem_cost
    acc.vdisk_cost += vdisk_cost

    if cpu_usage_pct is not None:
        acc.cpu_usage_sum += cpu_usage_pct
        acc.cpu_usage_samples += 1
    if mem_usage_pct is not None:
        acc.mem_usage_sum += mem_usage_pct
        acc.mem_usage_samples += 1
