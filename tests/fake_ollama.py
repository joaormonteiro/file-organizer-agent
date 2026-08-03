"""Dublê do binário `ollama`, roteirizado por variável de ambiente.

Nenhum teste depende do Ollama estar instalado (RNF-05). O modo vem de
`FAKE_OLLAMA_MODO`; o prompt recebido vai para `FAKE_OLLAMA_LOG`, para que o
teste possa provar que ele chegou por **stdin** e não por argv (RF-52).

Uso: este arquivo é chamado por um `.cmd` gerado em `tmp_path`, que é o que a
configuração aponta em `OLLAMA_BIN`.
"""

from __future__ import annotations

import json
import os
import sys
import time

RESPOSTA_VALIDA = {
    "categoria": "Documentos/Academico/UNIFESP/Matrizes-Curriculares",
    "nome_sugerido": "matriz-curricular-engenharia-computacao-2026",
    "confianca": 0.88,
    "motivo": "texto cita grade curricular e UNIFESP",
}


def _registrar(argv: list[str], prompt: str) -> None:
    destino = os.environ.get("FAKE_OLLAMA_LOG")
    if not destino:
        return
    registro = {"argv": argv, "prompt": prompt, "chamada_em": time.time()}
    with open(destino, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(registro, ensure_ascii=False) + "\n")


def _contar_chamadas() -> int:
    destino = os.environ.get("FAKE_OLLAMA_LOG")
    if not destino or not os.path.exists(destino):
        return 0
    with open(destino, encoding="utf-8") as fh:
        return sum(1 for linha in fh if linha.strip())


def main() -> int:
    argv = sys.argv[1:]
    modo = os.environ.get("FAKE_OLLAMA_MODO", "json_puro")

    if argv and argv[0] == "list":
        if modo == "modelo_ausente":
            print("NAME    ID    SIZE    MODIFIED")
            return 0
        if modo == "list_quebrado":
            print("erro", file=sys.stderr)
            return 1
        print("NAME         ID              SIZE      MODIFIED")
        print("phi3:mini    4f2222927938    2.2 GB    7 hours ago")
        return 0

    prompt = sys.stdin.read() if not sys.stdin.isatty() else ""
    chamadas_antes = _contar_chamadas()
    _registrar(argv, prompt)

    if modo == "flag_desconhecida":
        # instalação antiga: recusa a flag, mas responde normalmente sem ela
        if "--format" in argv:
            print("Error: unknown flag: --format", file=sys.stderr)
            return 1
        print("Claro, aqui esta:")
        print(json.dumps(RESPOSTA_VALIDA))
        return 0

    if modo == "dorme":
        time.sleep(float(os.environ.get("FAKE_OLLAMA_SONO", "30")))
        print(json.dumps(RESPOSTA_VALIDA))
        return 0

    if modo == "json_puro":
        print(json.dumps(RESPOSTA_VALIDA))
    elif modo == "cerca_markdown":
        print("```json")
        print(json.dumps(RESPOSTA_VALIDA))
        print("```")
    elif modo == "prosa_com_json":
        print("Claro! Analisei o documento e concluí o seguinte:")
        print(json.dumps(RESPOSTA_VALIDA))
        print("Espero ter ajudado.")
    elif modo == "lixo":
        print("Desculpe, não consegui identificar este arquivo.")
    elif modo == "lixo_depois_json":
        # falha na primeira chamada, acerta na segunda: prova o retry (RF-55)
        if chamadas_antes == 0:
            print("Hmm, deixa eu pensar melhor...")
        else:
            print(json.dumps(RESPOSTA_VALIDA))
    elif modo == "categoria_invalida":
        print(json.dumps({**RESPOSTA_VALIDA, "categoria": "Documentos/Inventada/Nao-Existe"}))
    elif modo == "confianca_invalida":
        print(json.dumps({**RESPOSTA_VALIDA, "confianca": "muito alta"}))
    elif modo == "confianca_fora_do_intervalo":
        print(json.dumps({**RESPOSTA_VALIDA, "confianca": 42}))
    elif modo == "nome_sujo":
        print(
            json.dumps(
                {
                    **RESPOSTA_VALIDA,
                    "nome_sugerido": 'matriz<>:"/\\|?*curricular 2026.docx',
                }
            )
        )
    elif modo == "ansi_do_terminal":
        # reproduz o que o ollama 0.32.5 REAL emite ao quebrar linha:
        # ESC[8D recua 8 colunas, ESC[K apaga a linha, e o pedaco e reimpresso
        corpo = json.dumps(RESPOSTA_VALIDA, ensure_ascii=False)
        marca = "curricular"
        pos = corpo.index(marca) + len(marca)
        sys.stdout.write("\x1b[?25l")
        sys.stdout.write(corpo[:pos] + "eletroni")
        sys.stdout.write("\x1b[8D\x1b[K\n")
        sys.stdout.write(corpo[pos:])
        sys.stdout.write("\x1b[?25h\n")
    elif modo == "eco_argv":
        print(json.dumps({**RESPOSTA_VALIDA, "motivo": " ".join(argv)}))
    elif modo == "erro":
        print("Error: model not found", file=sys.stderr)
        return 1
    else:  # pragma: no cover - modo desconhecido é erro de teste
        print(f"modo desconhecido: {modo}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
