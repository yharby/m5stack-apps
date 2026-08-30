# M5Stack UIFlow2 app registry - shared development commands
#
# All device access goes through tools/m5.py, which first breaks into the
# MicroPython REPL (UIFlow2 boots an asyncio launcher that owns the serial
# port, so a plain `mpremote connect` fails with "could not enter raw repl").
#
# Apps live one-file-per-app in device/apps/ and install to /flash/apps/<name>.py
# so the device APP.LIST menu shows them by name.

M5  := uv run python tools/m5.py
APP ?= translator

.PHONY: help setup catalog info ls apps push autorun rm-app run selftest sd-probe probe \
        repl reset logs clear-logs lint format check

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup:      ## install dev tooling into .venv
	uv sync

catalog:    ## list apps available in this repository
	@for app in device/apps/*.py; do basename "$$app" .py; done

info:       ## show port, firmware, memory, board info
	@$(M5) info

ls:         ## list the whole device filesystem
	@$(M5) ls

apps:       ## list apps installed on the device
	@$(M5) apps

push:       ## install app to /flash/apps/<name>.py        (APP=name)
	@$(M5) push $(APP)

autorun:    ## also install app as /flash/main.py (runs on boot)
	@$(M5) autorun $(APP)

rm-app:     ## delete an app from the device               (APP=name)
	@$(M5) rm-app $(APP)

run:        ## run app live, streaming its output here     (APP=name)
	@$(M5) run $(APP)

selftest:   ## translator end-to-end check: config, wifi, mic, OpenAI
	@$(M5) selftest

sd-probe:   ## mount and verify a CoreS3 SD card safely
	@$(M5) sd-probe

probe:      ## hardware smoke test (config, wifi, display, mic)
	@$(M5) probe

repl:       ## interactive MicroPython REPL (Ctrl-] to exit)
	@$(M5) repl

reset:      ## reboot the device back into UIFlow2
	@$(M5) reset

logs:       ## print the translator log                    (make logs n=100)
	@$(M5) logs -n $(or $(n),40)

clear-logs: ## delete the on-device app log
	@$(M5) clear-logs

lint:       ## lint device + host code
	uv run ruff check device/ tools/

format:     ## auto-format device + host code
	uv run ruff format device/ tools/

check:      ## format-check + lint (CI gate)
	uv run ruff format --check device/ tools/
	uv run ruff check device/ tools/
