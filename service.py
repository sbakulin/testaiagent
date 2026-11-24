"""Шаблон агента для платформы: замените тело `handler` своей логикой.

Обвязка `uai_agent_kit` даёт всё, что нужно платформе: `POST /agui` (запуск, поток событий), `GET /healthz`,
проверку токена и вызов модели через шлюз маскирования. Этот пример модель не вызывает и токены не тратит:
он показывает, как сообщать о шагах, промежуточном состоянии и результате.
"""
from __future__ import annotations

import time

from uai_agent_kit import Run, serve


def handler(run: Run) -> None:
    state = {"phase": "working", "steps_done": 0, "result": None}
    run.set_state(state)
    run.text("Принял задачу. Покажу два шага и верну результат.")

    run.tool_start("step-1", "count_words", {"text": run.task})
    time.sleep(0.5)
    words = len(run.task.split())
    run.tool_result("step-1", {"words": words})
    state.update(steps_done=1)
    run.set_state(state)

    run.check_cancelled()
    run.tool_start("step-2", "reverse_text", {"text": run.task})
    time.sleep(0.5)
    reversed_text = run.task[::-1]
    run.tool_result("step-2", {"reversed": reversed_text})

    state.update(phase="done", steps_done=2, result={"words": words, "reversed": reversed_text})
    run.set_state(state)
    run.text(f"Готово: в задаче {words} слов. Результат лежит в состоянии агента.")

    # Чтобы обратиться к модели через шлюз платформы:
    #   from uai_agent_kit import gateway_completion
    #   completion = gateway_completion()
    #   answer = completion(model="anthropic/claude-haiku-5-5", messages=[...], max_tokens=1000)
    # (добавьте litellm в requirements.txt)


if __name__ == "__main__":
    serve(handler, name="agent-template")
