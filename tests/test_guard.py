"""RF-33, RF-34, RF-35, RF-36 — Resource Guard."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from organizer import guard

RAIZ_PROJETO = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg(sandbox):
    """Config com os thresholds da spec (o sandbox usa 100 para neutralizar)."""
    return dataclasses.replace(
        sandbox.cfg,
        threshold_cpu=70.0,
        threshold_ram=80.0,
        threshold_gpu=60.0,
        threshold_vram=70.0,
    )


@pytest.mark.parametrize(
    "cpu,ram,gpu,vram,ocupado",
    [
        # sistema livre
        (10, 20, 5, 10, False),
        # cada métrica exatamente no limite: NÃO está ocupado (o operador é `>`)
        (70, 20, 5, 10, False),
        (10, 80, 5, 10, False),
        (10, 20, 60, 10, False),
        (10, 20, 5, 70, False),
        # cada métrica isoladamente acima do limite
        (70.1, 20, 5, 10, True),
        (10, 80.1, 5, 10, True),
        (10, 20, 60.1, 10, True),
        (10, 20, 5, 70.1, True),
        # combinação por `or`
        (99, 99, 99, 99, True),
    ],
)
def test_thresholds(cfg, cpu, ram, gpu, vram, ocupado):
    """RF-33: thresholds 70/80/60/70 combinados por `or`, como no snippet da spec."""
    recursos = guard.Recursos(cpu=cpu, ram=ram, gpu=gpu, vram=vram)
    assert guard.avaliar(cfg, recursos) is ocupado


def test_sistema_ocupado_usa_snapshot(cfg, monkeypatch):
    monkeypatch.setattr(guard, "_ler_cpu_ram", lambda intervalo=1.0: (99.0, 10.0))
    monkeypatch.setattr(guard, "_ler_gpu", lambda indice=0: (0.0, 0.0))
    ocupado, recursos = guard.sistema_ocupado(cfg, intervalo_cpu=0)
    assert ocupado is True
    assert recursos.cpu == 99.0
    assert "cpu=99" in recursos.resumo()


def test_sem_gpu_degrada_para_zero(cfg, monkeypatch):
    """RF-34: qualquer falha NVML vira gpu=0, vram=0, fonte='indisponivel'."""

    def explode(indice=0):
        raise RuntimeError("NVMLError_LibraryNotFound")

    monkeypatch.setattr(guard, "_ler_cpu_ram", lambda intervalo=1.0: (5.0, 5.0))
    monkeypatch.setattr(guard, "_ler_gpu", explode)

    recursos = guard.snapshot(cfg, intervalo_cpu=0)
    assert recursos.gpu == 0.0
    assert recursos.vram == 0.0
    assert recursos.fonte_gpu == guard.FONTE_INDISPONIVEL
    assert guard.avaliar(cfg, recursos) is False


def test_pynvml_ausente_nao_quebra(cfg, monkeypatch):
    """RF-34: até `ImportError` de `pynvml` degrada graciosamente."""

    def sem_modulo():
        raise ImportError("No module named 'pynvml'")

    monkeypatch.setattr(guard, "_ler_cpu_ram", lambda intervalo=1.0: (5.0, 5.0))
    monkeypatch.setattr(guard, "_nvml", sem_modulo)
    recursos = guard.snapshot(cfg, intervalo_cpu=0)
    assert recursos.fonte_gpu == guard.FONTE_INDISPONIVEL


class _NvmlFake:
    """Dublê de `pynvml` que registra init/shutdown."""

    def __init__(self, gpu=42.0, usada=2048, total=8192, falhar=False):
        self.chamadas: list[str] = []
        self.gpu = gpu
        self.usada = usada
        self.total = total
        self.falhar = falhar

    def nvmlInit(self):
        self.chamadas.append("init")

    def nvmlShutdown(self):
        self.chamadas.append("shutdown")

    def nvmlDeviceGetHandleByIndex(self, indice):
        if self.falhar:
            raise RuntimeError("NVMLError_GpuIsLost")
        return f"handle-{indice}"

    def nvmlDeviceGetUtilizationRates(self, handle):
        return type("Uso", (), {"gpu": self.gpu})()

    def nvmlDeviceGetMemoryInfo(self, handle):
        return type("Mem", (), {"used": self.usada, "total": self.total})()


def test_leitura_de_gpu_converte_como_a_spec(monkeypatch):
    """RF-33: `gpu` vem em % e `vram` é `used/total*100`."""
    fake = _NvmlFake(gpu=42.0, usada=2048, total=8192)
    monkeypatch.setattr(guard, "_nvml", lambda: fake)
    gpu, vram = guard._ler_gpu(0)
    assert gpu == 42.0
    assert vram == pytest.approx(25.0)


def test_vram_com_total_zero(monkeypatch):
    fake = _NvmlFake(usada=0, total=0)
    monkeypatch.setattr(guard, "_nvml", lambda: fake)
    assert guard._ler_gpu(0) == (42.0, 0.0)


def test_shutdown_sempre_chamado(monkeypatch):
    """RF-35: `nvmlInit`/`nvmlShutdown` pareados em try/finally mesmo com erro."""
    fake = _NvmlFake(falhar=True)
    monkeypatch.setattr(guard, "_nvml", lambda: fake)
    with pytest.raises(RuntimeError):
        guard._ler_gpu(0)
    assert fake.chamadas == ["init", "shutdown"]

    ok = _NvmlFake()
    monkeypatch.setattr(guard, "_nvml", lambda: ok)
    guard._ler_gpu(0)
    assert ok.chamadas == ["init", "shutdown"]


def test_leitura_de_cpu_ram_usa_psutil(monkeypatch):
    class _PsutilFake:
        @staticmethod
        def cpu_percent(interval=1.0):
            return 33.0

        @staticmethod
        def virtual_memory():
            return type("Mem", (), {"percent": 44.0})()

    monkeypatch.setattr(guard, "_psutil", lambda: _PsutilFake)
    assert guard._ler_cpu_ram(0) == (33.0, 44.0)


def test_biblioteca_gpu_abandonada_nao_e_dependencia():
    """RF-36: a biblioteca abandonada em 2018 não aparece fora de `docs/`.

    O nome é montado em runtime justamente para que este arquivo de teste não
    seja, ele mesmo, uma ocorrência do termo procurado.
    """
    proibido = "gpu" + "til"
    alvos = [
        RAIZ_PROJETO / "requirements.txt",
        RAIZ_PROJETO / "requirements-dev.txt",
        RAIZ_PROJETO / "requirements-semantic.txt",
    ]
    alvos += sorted((RAIZ_PROJETO / "organizer").glob("*.py"))
    alvos += sorted((RAIZ_PROJETO / "tests").glob("*.py"))
    alvos += sorted(RAIZ_PROJETO.glob("*.py"))
    for arquivo in alvos:
        conteudo = arquivo.read_text(encoding="utf-8").lower()
        assert proibido not in conteudo, f"{arquivo} menciona a biblioteca abandonada"
    requisitos = (RAIZ_PROJETO / "requirements.txt").read_text(encoding="utf-8")
    assert "nvidia-ml-py==" in requisitos, "a wheel oficial da NVIDIA precisa estar fixada"
    for linha in requisitos.splitlines():
        if linha.strip() and not linha.startswith("#"):
            assert not linha.startswith("pynvml"), "o shim `pynvml` não é dependência"


def test_modulo_nvml_importa_sem_aviso_de_depreciacao():
    """RNF-18: `nvidia-ml-py` publica o módulo `pynvml` — e sem FutureWarning.

    O shim `pynvml` do PyPI faria este teste falhar: ele emite um
    `FutureWarning` no import pedindo justamente para instalar `nvidia-ml-py`.
    """
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        warnings.simplefilter("error", DeprecationWarning)
        import importlib

        import pynvml

        importlib.reload(pynvml)
    import importlib.metadata as md

    assert md.version("nvidia-ml-py")
    with pytest.raises(md.PackageNotFoundError):
        md.version("pynvml")


def test_leitores_reais_importam_as_bibliotecas():
    """RNF-18: `psutil` e `pynvml` estão instalados e são importados sob demanda."""
    import psutil as psutil_real

    assert guard._psutil() is psutil_real
    import pynvml as pynvml_real

    assert guard._nvml() is pynvml_real
