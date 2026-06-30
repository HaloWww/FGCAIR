# FGCAir Home Assistant 自定义集成

FGCAir 是一个面向 FGCAir/机智云私有云空调的 Home Assistant 自定义集成。集成会创建标准 `climate` 实体，并可通过 Home Assistant HomeKit Bridge 同步到 HomeKit。

本项目不会在文档中公开任何真实账号、密码、token、DID、MAC、家庭 IP 或 Home Assistant 地址。下面所有示例值均为占位符。

## 功能

- 通过配置向导登录 FGCAir 账号。
- 自动保存 token，并在 token 失效时重新登录。
- 选择需要接入的室内机。
- 创建 Home Assistant `climate` 实体。
- 支持开关、目标温度、制冷、制热、除湿和风速控制。
- 支持选择当前室温来源，可使用空调自身室温或 HA 中已有温度实体。
- 使用 MQTT TLS 定时同步设备状态。
- MQTT 状态同步只处理开关、目标温度和房间温度，不处理模式和风速。
- 控制命令按单个属性逐条下发，降低设备拒绝或丢命令概率。
- 可通过 HomeKit Bridge 桥接到 Apple Home。

## 安装

把仓库中的目录复制到 Home Assistant 配置目录：

```text
custom_components/fgcair
```

复制后重启 Home Assistant。

## 添加集成

在 Home Assistant 中打开：

```text
设置 -> 设备与服务 -> 添加集成 -> FGCAir
```

配置向导参数：

| 参数 | 说明 | 示例 |
| --- | --- | --- |
| `username` | FGCAir/机智云账号，通常是手机号或用户名。 | `<your_username>` |
| `password` | FGCAir/机智云账号密码。 | `<your_password>` |
| `selected_dids` | 要创建实体的室内机列表。配置向导会显示可选设备。 | `<indoor_device_id>` |

配置完成后会生成每台室内机对应的 `climate` 实体和“当前室温来源”选择实体。

## 插件设置

进入已添加的 FGCAir 集成，打开“配置”或“选项”可调整运行参数。

| 参数 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- |
| `update_interval` | `60` 秒 | `10` 到 `3600` 秒 | MQTT 状态查询间隔。间隔越短，HA/HomeKit 中的开关和温度状态刷新越快。 |

## Climate 实体

每台室内机会创建一个标准 `climate` 实体。

支持的 Home Assistant 功能：

| 功能 | 说明 |
| --- | --- |
| 开机/关机 | 写入 `Power_indoor_PK4`。 |
| 目标温度 | 写入 `Temp_indoor_PK4`，范围 `18` 到 `30` 摄氏度，步进 `0.5`。 |
| HVAC 模式 | 支持 `off`、`cool`、`heat`、`dry`。 |
| 风速 | 支持 `自动`、`1档`、`2档`、`3档`、`4档`、`5档`、`6档`。 |

控制时会按属性逐个调用云端控制接口。例如同时调整开关和目标温度时，会拆成多个单属性请求顺序发送。

## MQTT 状态同步

集成通过 FGCAir 私有云 MQTT TLS 通道定时读取设备状态。

MQTT 只同步以下状态到 Home Assistant：

| MQTT 状态 | HA 属性 | 说明 |
| --- | --- | --- |
| 开关状态 | `Power_indoor_PK4` | 用于刷新 `climate` 的 `hvac_mode` 是否为 `off`。 |
| 目标温度 | `Temp_indoor_PK4` | 用于刷新 `target_temperature`。 |
| 房间温度 | `Roomtemp_indoor_PK4` | 用于刷新 `current_temperature`。 |

MQTT 不同步以下状态：

| 状态 | 原因 |
| --- | --- |
| 模式 | 协议字段未稳定确认，避免误报制冷、制热或除湿。 |
| 风速 | 协议字段未稳定确认，避免误报风速。 |

## 当前室温来源

每台室内机会创建一个“当前室温来源”选择实体。

可选项：

| 选项 | 说明 |
| --- | --- |
| `自身室温` | 使用 MQTT 读取到的空调自身房间温度。 |
| HA 温度实体 | 使用 Home Assistant 中已有的 `sensor` 温度实体或 `climate` 实体的 `current_temperature`。 |

选择外部温度实体后，集成会立即读取该实体当前温度，并在该实体状态变化时刷新 FGCAir `climate` 的当前温度。

## 空调风速

风速整合在 `climate` 实体的 `fan_mode` 中，不会额外创建其他设备。

风速支持：

| 档位 | 下发值 | 说明 |
| --- | --- | --- |
| `自动` | `0` | 自动风速。 |
| `1档` | `1` | 最低风速。 |
| `2档` | `2` | 低风速。 |
| `3档` | `3` | 中风速。 |
| `4档` | `4` | 高风速。 |
| `5档` | `5` | 中高风速。 |
| `6档` | `6` | 最高风速。 |

因为目前无法从 MQTT 稳定读取真实风速，集成不会用 MQTT 同步风速状态。它只保存最后一次通过 HA 或 HomeKit 下发的风速，用于展示和继续控制。

## HomeKit Bridge

集成创建标准 Home Assistant `climate` 实体。要桥接到 HomeKit，请在 HomeKit Bridge 集成中选择对应的 `climate` 实体。

HomeKit 同步建议：

| 参数 | 建议 |
| --- | --- |
| `update_interval` | 测试时可设为 `10` 或 `30` 秒，稳定后可改回 `60` 秒或更长。 |
| 同步字段 | 当前只建议依赖开关状态、目标温度和当前室温。 |

HomeKit 中如果显示风速控制，可以按 7 段理解为 `自动`、`1档`、`2档`、`3档`、`4档`、`5档`、`6档`。风速可以下发到设备，但真实风速状态不会通过 MQTT 回读。

Apple Home 对部分空调模式的展示有限制，Home Assistant 中可用的模式不一定会在 HomeKit 中以相同方式显示。

## 服务

### `fgcair.refresh_token`

立即使用配置中的账号密码重新登录，并刷新保存的 token。

参数：无。

### `fgcair.test_control`

直接调用 FGCAir 控制接口，适合在开发者工具中排查设备是否可控。

| 参数 | 必填 | 说明 | 示例 |
| --- | --- | --- | --- |
| `did` | 是 | 设备 DID。请使用自己 HA 中的真实设备 ID，不要公开分享。 | `<device_did>` |
| `pk_index` | 否 | 数据点后缀，默认 `4`。 | `4` |
| `power` | 否 | 开关。 | `true` |
| `mode` | 否 | 模式。`0` 自动，`1` 制冷，`2` 除湿，`3` 通风，`4` 制热。 | `1` |
| `temperature` | 否 | 目标温度，范围 `18` 到 `30`。 | `26` |
| `speed` | 否 | 风速。`0` 自动，`1` 超低，`2` 低档，`3` 中档，`4` 高档，`5` 中高，`6` 超高。 | `0` |

示例：

```yaml
service: fgcair.test_control
data:
  did: "<device_did>"
  pk_index: 4
  power: true
  temperature: 26
```

## 参数速查

| 名称 | 位置 | 说明 |
| --- | --- | --- |
| `username` | 配置向导 | FGCAir/机智云账号。 |
| `password` | 配置向导 | FGCAir/机智云密码。 |
| `selected_dids` | 配置向导 | 选择需要接入的室内机。 |
| `update_interval` | 插件选项 | MQTT 状态查询间隔，单位秒。 |
| `temp_source_entity_id` | 状态缓存 | 当前室温来源实体 ID，由选择实体维护。 |
| `Power_indoor_PK4` | 控制/状态 | 开关状态。 |
| `Temp_indoor_PK4` | 控制/状态 | 目标温度。 |
| `Roomtemp_indoor_PK4` | 状态 | 房间温度原始值。 |
| `Mode_indoor_PK4` | 控制 | 模式控制。MQTT 不更新该字段。 |
| `Speed_indoor_PK4` | 控制 | 风速控制。MQTT 不更新该字段。 |

## 隐私与脱敏

请不要在 issue、日志或截图中公开以下信息：

- FGCAir/机智云账号和密码。
- token、uid、session 信息。
- 设备 DID、gateway DID、passcode、MAC、mesh ID。
- 家庭公网 IP、内网 IP、Home Assistant 地址。
- HomeKit 配对码或实体唯一 ID。

提交问题时建议使用以下占位符：

```text
username: <redacted_username>
token: <redacted_token>
did: <redacted_device_did>
gateway_did: <redacted_gateway_did>
mac: <redacted_mac>
home_assistant_url: <redacted_ha_url>
```

## 排查

如果配置向导或状态同步异常，请检查 Home Assistant 日志中的 `custom_components.fgcair` 记录。

常见方向：

| 现象 | 检查项 |
| --- | --- |
| 配置向导无法打开 | 确认文件已复制到 `custom_components/fgcair` 并重启 HA。 |
| 登录失败 | 检查账号密码是否正确，或调用 `fgcair.refresh_token`。 |
| HomeKit 状态不刷新 | 降低 `update_interval` 测试，例如 `10` 或 `30` 秒。 |
| 目标温度/开关不同步 | 检查 MQTT 后台是否有 `FGCAir MQTT` 相关日志。 |
| 当前室温不对 | 检查“当前室温来源”选择实体是否选择了正确温度实体。 |

## 图标

集成图标按 Home Assistant 自定义集成品牌图规范放置在：

```text
custom_components/fgcair/brand/icon.png
custom_components/fgcair/brand/logo.png
```

## 支持项目

这个集成是在实际设备调试和反复验证中慢慢做出来的。为了摸清 FGCAir 的控制和状态同步逻辑，开发过程中消耗了不少云端 token，也花了不少时间做抓包、验证和 HomeKit 适配。

如果这个项目刚好帮你解决了问题，并且你愿意支持后续维护，可以随意打赏一点。完全自愿，不影响使用，也没有任何强制要求。

| 微信 | 支付宝 |
| --- | --- |
| ![微信打赏](docs/assets/wechat-donation.jpeg) | ![支付宝打赏](docs/assets/alipay-donation.jpeg) |
