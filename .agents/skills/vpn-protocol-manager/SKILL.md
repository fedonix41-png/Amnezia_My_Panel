---
name: "vpn-protocol-manager"
description: "Активировать при добавлении нового VPN-протокола (Xray, WireGuard, AWG и др.) или редактировании существующих менеджеров."
---
# Менеджеры протоколов

1. **Утиная типизация**: Базового класса нет. Новый менеджер должен реализовывать общий интерфейс: `install_protocol`, `remove_container`, `get_server_status`, `check_docker_installed`, `get_clients`, `add_client` и т.д.
2. **Сигнатура**: Все клиентские методы (add_client, remove_client и т.д.) **обязаны** первым аргументом принимать `protocol_type` (чтобы работал диспетчер `_manager_call`). Исключение — single-instance протоколы (WireGuard), но это требует хардкода в `_manager_call`.
3. **Регистрация**: Новый протокол должен быть добавлен в `BASE_PROTOCOLS` (и `MULTI_INSTANCE_PROTOCOLS`, если нужно) в `app.py`, а его менеджер добавлен в диспетчер `get_protocol_manager` (с ленивым импортом).
4. **Изоляция**: Все протоколы деплоятся только как Docker-контейнеры.