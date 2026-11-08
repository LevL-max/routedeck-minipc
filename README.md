# routedeck-minipc

Домашний роутер на мини-ПК с двумя Ethernet-портами и Ubuntu Server 22.04. Установка одной командой.

Что умеет:
- **Режимы трафика LAN:** DIRECT (обычный NAT), AWG (AmneziaWG), TUN (sing-box), MIHOMO (Mihomo в режиме TUN). Режим переключается в панели.
- **Панель RouteDeck** на `http://192.168.50.1:8080`: режимы, профили, файлы конфигураций, обновления компонентов.
- **router-wan:** Ethernet с резервом через 4G-модем (ModemManager, QMI), принудительный выбор WAN, выбор частотных бэндов, предупреждение о «белых списках» мобильного оператора.
- **Сторожа:** AWG и TUN перезапускают и переключают профиль при обрыве; kill switch не выпускает LAN мимо выбранного режима.
- **DHCP в LAN**, обновление ядра и компонентов с откатом (Router Updater).

Файлы роутера взяты с рабочего мини-ПК байт в байт. Меняются только имена портов: установщик подставляет порты вашего ПК.

## Что нужно

- ПК x86-64 (amd64) с **двумя Ethernet-портами**. 4G-модем по желанию: проверены Telit LM960 и Sierra MC7455 в режиме QMI.
- **Ubuntu Server 22.04**. При установке Ubuntu создайте пользователя `ubuntu`.
- Интернет на WAN-порту (DHCP) во время установки.

LAN всегда `192.168.50.1/24`: DHCP раздаёт `.100–.200`, DNS 1.1.1.1 и 8.8.8.8.

## Установка: простой роутер, свои VPN потом

1. Поставьте Ubuntu Server 22.04, подключите интернет в порт, который будет WAN.
2. Скачайте репозиторий на ПК:
   ```bash
   git clone https://github.com/LevL-max/routedeck-minipc.git
   ```
3. Сделайте свой профиль из шаблона и поправьте `host.env` (название и цвет в панели, режим):
   ```bash
   cp -r routedeck-minipc/hosts/example ~/my-router && nano ~/my-router/host.env
   ```
4. Прогоните установку вхолостую, она ничего не меняет:
   ```bash
   sudo routedeck-minipc/install.sh --profile ~/my-router --no-secrets --dry-run
   ```
5. Установите. Скрипт покажет порты (WAN — тот, где интернет) и спросит подтверждение:
   ```bash
   sudo routedeck-minipc/install.sh --profile ~/my-router --no-secrets
   ```
6. Перезагрузите ПК и проверьте:
   ```bash
   sudo routedeck-minipc/install.sh --check
   ```
7. Подключите компьютер в LAN-порт. Он получит адрес из `192.168.50.0/24`, панель откроется на http://192.168.50.1:8080.

Без секретов роутер работает в режиме **DIRECT**. Чтобы включить VPN-режимы, положите свои конфиги в пути из таблицы ниже, затем выберите режим в панели. Для Xray ещё выполните `sudo systemctl enable --now xray xray-recover.timer`.

## Установка своего ПК с ключами (профиль + секреты)

Профиль — это каталог с файлами:
- `host.env` — название и цвет в панели, режим, имена портов исходного ПК;
- `authorized_keys` — SSH-ключи для пользователя `ubuntu`, по желанию;
- `secrets.tar.gpg` — зашифрованный архив конфигов, по желанию.

Держите профили в **закрытом** репозитории, не в этом. Установка:

```bash
sudo routedeck-minipc/install.sh --profile ~/my-profiles/pc2
```

Скрипт спросит пароль от архива, разложит файлы с нужными правами и заменит имя WAN-порта внутри конфигов Xray, Mihomo и sing-box на порт нового ПК.

Ключи в архиве **живые**. Два роутера с одним AWG-ключом выбивают друг друга с сервера, поэтому клон ставят **вместо** оригинала.

## Где роутер ждёт конфиги

| Путь | Владелец | Права | Что это |
|---|---|---|---|
| `/etc/amnezia/amneziawg/temp_1/wg0.conf`, `temp_2/wg0.conf` | ubuntu:ubuntu | 600 | два AWG-профиля: основной и запасной |
| `/etc/amnezia/amneziawg/temp_1.conf` → `temp_1/wg0.conf` (то же для temp_2) | ubuntu:ubuntu | ссылка | имена профилей для `awg-quick` |
| `/etc/amnezia/amneziawg/variants/temp_1.conf`, `temp_2.conf` | ubuntu:ubuntu | 640 | копии профилей для карточки «Configuration files» в панели |
| `/etc/amnezia/amneziawg/wg0.conf`, `active` | ubuntu:ubuntu | 640, 644 | активный профиль и его имя (`temp_1`) |
| `/etc/router/watchdog/temp_1/wg0.conf`, `temp_2/…`, `active` | root:root | 700, 600 | копии для сторожа AWG |
| `/etc/sing-box/config.json`, `/etc/sing-box/configs/tun_1.json`, `tun_2.json` | ubuntu:sing-box | 664, 640 | режим TUN: активный и два профиля |
| `/etc/mihomo/config.yaml` | root:root | 640 | Mihomo: DNS `127.0.0.1:1053`, контроллер `192.168.50.1:9090` |
| `/etc/xray/config.json`, `nodes.json` | ubuntu:router-xray | 640 | SOCKS-мосты Xray для Mihomo |
| `/etc/router/wan.json` | root:root | 600 | WAN и 4G: APN, резервирование |

Когда файлы на месте, панель умеет их заменять и проверять. Создать файл с нуля из панели нельзя.

## Что делает install.sh

1. **Проверки:** Ubuntu 22.04, amd64, root, два Ethernet-порта. WAN — порт с маршрутом по умолчанию, LAN — второй. Задать вручную: `--wan`, `--lan`.
2. **Пакеты** из `lock/packages.txt`:
   - PPA Amnezia (модуль AmneziaWG через DKMS) и ядро HWE 6.8 с заголовками;
   - isc-dhcp-server, iptables-persistent, ModemManager и QMI-утилиты.

   Пока идёт установка, новые службы не стартуют. ModemManager помечается как вручную установленный, чтобы `apt autoremove` его не снёс.
3. **Бинарники** из `lock/binaries.lock`. У каждого проверяется sha256 и архива, и самого файла:
   - mihomo v1.19.33;
   - sing-box LX 1.14.4-lx.32 поверх пакета sing-box 1.14.2 (через dpkg-divert);
   - xray v26.10.31;
   - веб-панель Mihomo (Zashboard, необязательна).
4. **Пользователи** `ubuntu` и `router-xray`.
5. **Файлы роутера** из `rootfs/` по `rootfs.manifest`, с правами и владельцами. Прочие netplan-файлы откладываются в сторону.
6. **Секреты** профиля, если есть, и SSH-ключи из профиля.
7. **Имя хоста и состояния служб** по `lock/units.txt`.
8. **Перезагрузка:** сеть, sysctl, фаервол и выбранный режим поднимаются при загрузке.

Всё, что установка заменила, сохраняется в `/var/backups/routedeck/install-<время>/` вместе со скриптами отката `undo-rootfs.sh` и `undo-secrets.sh`. Пакеты и бинарники откат не удаляет.

## Безопасность

- **У панели нет пароля**, и через неё можно скачать конфиги. Она отвечает только LAN и сетям из `TRUSTED_NET` в профиле. Не открывайте порт 8080 наружу.
- Пользователь `ubuntu` получает `sudo` без пароля (`/etc/sudoers.d/99-ubuntu-nopasswd`).
- **Никогда не коммитьте открытые архивы и конфиги** в публичный репозиторий. `.gitignore` здесь их уже отсекает.

## Что проверено

На исходном мини-ПК:
- **Сборка равна живой системе:** все 135 позиций (102 файла, 32 каталога, 1 ссылка) совпали по sha256, правам и владельцам.
- **Пробный прогон `--dry-run`** на нём же ничего не изменил бы, кроме трёх ожидаемых пунктов: добавил бы пакет `linux-tools-generic-hwe-22.04`, отложил бы старый `00-installer-config.yaml` и пометил бы ModemManager как установленный вручную.
- **Подстановка портов:** сборка с другими именами портов не содержит старых.

**Не проверено:**
- установка на чистую машину целиком;
- работа без 4G-модема или с другим модемом;
- путь «без секретов, свои конфиги» из таблицы выше.

## Обновить репозиторий с рабочего ПК

```bash
ssh ubuntu@ПК "tr -d '\r' | sudo bash -s" < tools/snapshot.sh > /вне/репо/pc.tgz   # только чтение; в архиве секреты
python3 tools/import_snapshot.py --repo . --base pc --snapshot pc=/вне/репо/pc.tgz \
    --profiles-out ~/my-profiles --secrets-out /вне/репо/plain
python3 lib/render.py verify --host-env ~/my-profiles/pc/host.env --snapshot-manifest <meta/manifest.txt из снимка>
bash tools/encrypt-secrets.sh ~/my-profiles /вне/репо/plain/pc.tar      # пароль вводите сами
```

## Состав

```
install.sh                  установщик (--dry-run, --check)
lib/render.py               сборка rootfs для хоста, установка с бэкапом, сверка со снимком
rootfs/ + rootfs.manifest   файлы роутера с метками @@WAN_IF@@, @@LAN_IF@@ и др.
hosts/example/host.env      шаблон профиля
lock/                       версии бинарников, пакеты, состояния служб
tools/                      снимок с рабочего ПК, импорт, шифрование секретов
```
