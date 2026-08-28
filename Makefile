# M5Stack CoreS3 translator - dev commands
# All device access goes through tools/m5.py (handles the UIFlow2 REPL break-in).

M5 := uv run python tools/m5.py

.PHONY: help setup info ls logs clear-logs push run repl reset probe lint

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup:      ## install dev tooling into .venv
	uv sync

info:       ## show port, firmware, board info
	@$(M5) info

ls:         ## list device filesystem
	@$(M5) ls

logs:       ## print the on-device app log
	@$(M5) logs

clear-logs: ## delete the on-device app log
	@$(M5) clear-logs

push:       ## copy device/main.py to /flash/main.py
	@$(M5) push

run:        ## run device/main.py live, streaming output to this terminal
	@$(M5) run

repl:       ## interactive REPL (Ctrl-] to exit)
	@$(M5) repl

reset:      ## reboot the device back into UIFlow2
	@$(M5) reset

probe:      ## hardware smoke test (mic, wifi, display, config)
	@$(M5) probe

lint:       ## lint host-side tooling
	uv run ruff check tools/
