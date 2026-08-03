"""Resource Guard: adia o processamento quando o PC está ocupado.

⚠ DIVERGÊNCIA 1 da ARQUITETURA: o guard roda dentro do **processo filho**, no
topo do `ingest`, e não no watcher. `psutil.cpu_percent(interval=1)` bloqueia um
segundo e `psutil`/`pynvml` custam ~2 MB residentes — rodá-los no watcher
violaria o princípio de zero RAM em idle.

⚠ DIVERGÊNCIA 3: usamos `pynvml`. A alternativa oferecida pela spec está
abandonada desde 2018 e é publicada só como sdist (ver ARQUITETURA §6).
A semântica do snippet da spec é preservada 1:1.

Requisitos cobertos: RF-33, RF-34, RF-35, RF-36.
"""

from __future__ import annotations

from dataclasses import dataclass

FONTE_NVML = "nvml"
FONTE_INDISPONIVEL = "indisponivel"


@dataclass(frozen=True)
class Recursos:
    """Leitura instantânea dos quatro indicadores da spec."""

    cpu: float
    ram: float
    gpu: float
    vram: float
    fonte_gpu: str = FONTE_INDISPONIVEL

    def resumo(self) -> str:
        return (
            f"cpu={self.cpu:.0f} ram={self.ram:.0f} "
            f"gpu={self.gpu:.0f} vram={self.vram:.0f} fonte={self.fonte_gpu}"
        )


# --------------------------------------------------------------------------- #
# Leitores de baixo nível — isolados para poderem ser trocados nos testes
# --------------------------------------------------------------------------- #


def _psutil():
    """Importa `psutil` só quando alguém realmente vai medir."""
    import psutil

    return psutil


def _nvml():
    """Importa `pynvml` só quando alguém realmente vai medir."""
    import pynvml

    return pynvml


def _ler_cpu_ram(intervalo: float = 1.0) -> tuple[float, float]:
    """CPU% (média no intervalo) e RAM% — equivalente ao snippet da spec."""
    psutil = _psutil()
    cpu = float(psutil.cpu_percent(interval=intervalo))
    ram = float(psutil.virtual_memory().percent)
    return cpu, ram


def _ler_gpu(indice: int = 0) -> tuple[float, float]:
    """GPU% e VRAM% via NVML. `nvmlInit`/`nvmlShutdown` sempre pareados (RF-35).

    Equivalências com o snippet da spec:
    `gpus[0].load*100`      -> `nvmlDeviceGetUtilizationRates(h).gpu`
    `gpus[0].memoryUtil*100`-> `mem.used / mem.total * 100`
    """
    nvml = _nvml()
    nvml.nvmlInit()
    try:
        handle = nvml.nvmlDeviceGetHandleByIndex(indice)
        uso = nvml.nvmlDeviceGetUtilizationRates(handle)
        memoria = nvml.nvmlDeviceGetMemoryInfo(handle)
        gpu = float(uso.gpu)
        total = float(getattr(memoria, "total", 0) or 0)
        usada = float(getattr(memoria, "used", 0) or 0)
        vram = (usada / total * 100.0) if total else 0.0
        return gpu, vram
    finally:
        nvml.nvmlShutdown()


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


def snapshot(cfg, intervalo_cpu: float = 1.0) -> Recursos:
    """Lê os quatro indicadores. GPU indisponível degrada para zero (RF-34)."""
    cpu, ram = _ler_cpu_ram(intervalo_cpu)
    try:
        gpu, vram = _ler_gpu(cfg.gpu_index)
        fonte = FONTE_NVML
    except Exception:
        gpu, vram, fonte = 0.0, 0.0, FONTE_INDISPONIVEL
    return Recursos(cpu=cpu, ram=ram, gpu=gpu, vram=vram, fonte_gpu=fonte)


def avaliar(cfg, recursos: Recursos) -> bool:
    """Combinação por `or` dos quatro thresholds, idêntica à spec (RF-33)."""
    return (
        recursos.cpu > cfg.threshold_cpu
        or recursos.ram > cfg.threshold_ram
        or recursos.gpu > cfg.threshold_gpu
        or recursos.vram > cfg.threshold_vram
    )


def sistema_ocupado(cfg, intervalo_cpu: float = 1.0) -> tuple[bool, Recursos]:
    """`(ocupado, recursos)` — a pergunta que o `ingest` faz antes de agir."""
    recursos = snapshot(cfg, intervalo_cpu)
    return avaliar(cfg, recursos), recursos
