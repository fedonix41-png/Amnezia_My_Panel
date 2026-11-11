---
name: "vpn-protocol-manager"
description: "Активировать при добавлении нового VPN-протокола или редактировании существующих менеджеров в managers/."
---
# Менеджеры протоколов

1. **Неоднородность сигнатур (Важно)**: Единого базового класса нет. Перед вызовом или изменением метода всегда проверяй его реальную сигнатуру:
   - `_manager_call(manager, method, protocol, ...)` передаёт `protocol` первым аргументом для всех менеджеров, **кроме** `WireGuardManager`.
   - `XrayManager` получает `protocol` в конструкторе, но часть клиентских методов также принимает `protocol`.
   - Установка и удаление вызываются по веткам в `routers/protocols.py` (`install_protocol` / `remove_container`) — не заменяй их слепым общим вызовом.
2. **Вспомогательные модули**: Атомарная работа с `clientsTable` вынесена в `managers/clients_table.py`, подсчёт трафика — в `managers/traffic_counters.py`, обновление AWG-плагина — в `managers/awg_plugin_updater.py`, генерация клиентских конфигов `vpn://` — в `managers/amnezia_config.py`.
3. **Регистрация**: Протоколы регистрируются в `BASE_PROTOCOLS` и `MULTI_INSTANCE_PROTOCOLS` в `app.py`, фабрика — `get_protocol_manager(ssh, protocol)`.
4. **Глубокий контекст (читать по требованию)**:
   - `docs/terrain/02-core.md` — реестр протоколов и фабрика менеджеров.
   - `docs/terrain/05-gotchas.md` — таблица различий сигнатур менеджеров.
   - `docs/terrain/06-vpn-formats.md` — форматы `vpn://`, QR и обфускации AWG.
   - `docs/terrain/07-deployment.md` — чеклист добавления нового протокола.
