# 安装与使用指南

## 环境

- Python 3.10 或更新版本。
- Git，用于克隆仓库；也可从 GitHub 下载 ZIP，解压后进入含有 `workbench` 文件夹的目录。
- 浏览器。

运行依赖均为 Python 标准库。不需要 `pip install`、模型 API Key 或管理员权限。

```bash
git clone https://github.com/jia1974346649-beep/enterprise-agent-workbench.git
cd enterprise-agent-workbench
python --version
python -m workbench --port 8765
```

打开 [http://localhost:8765](http://localhost:8765)。如果使用 ZIP，省略前两条命令，先在解压后的项目根目录打开终端。

只在本机运行，保持终端开启。按 `Ctrl+C` 停止。若 `python` 不是已安装的 Python 3 命令，请使用 `python3`。

## 默认模式

界面明确显示离线模式。它通过确定规则选择工具，使读者在没有模型账户和外部服务时仍能观察流程。

离线演示只能处理设计范围内的请求，不具备通用自然语言理解能力。无法处理的问题应返回明确结果，不能把演示规则的成功表现解释为真实模型的推理水平。

所有样例都是合成数据，请勿上传或输入真实企业文件、密钥或客户信息。

### 恢复默认离线模式

首次使用且未设置环境变量时，默认就是 `offline`。如果此前在当前终端启用过 Ollama，停止正在运行的服务，再执行以下命令，避免残留环境变量改变本次演示模式。

Windows PowerShell：

```powershell
$env:WORKBENCH_MODE = "offline"
Remove-Item Env:WORKBENCH_OLLAMA_MODEL -ErrorAction SilentlyContinue
Remove-Item Env:WORKBENCH_OLLAMA_URL -ErrorAction SilentlyContinue
python -m workbench --port 8765
```

macOS / Linux 的 POSIX shell：

```bash
export WORKBENCH_MODE=offline
unset WORKBENCH_OLLAMA_MODEL WORKBENCH_OLLAMA_URL
python -m workbench --port 8765
```

### 可选：接入自己的本地 Ollama

此步骤不是运行默认演示的前提。你需要先自行安装并启动 Ollama，并在本机准备一个支持工具调用的模型。本仓库不会替你下载模型，也未验证具体模型的推理效果。

将下方的 `your-installed-tool-model` 替换为你本机已安装且支持工具调用的实际模型名称。默认使用本机 Ollama 地址，无须显式设置 `WORKBENCH_OLLAMA_URL`。先停止原服务，再在同一终端配置并启动。

Windows PowerShell：

```powershell
$env:WORKBENCH_MODE = "ollama"
$env:WORKBENCH_OLLAMA_MODEL = "your-installed-tool-model"
Remove-Item Env:WORKBENCH_OLLAMA_URL -ErrorAction SilentlyContinue
python -m workbench --port 8765
```

macOS / Linux 的 POSIX shell：

```bash
export WORKBENCH_MODE=ollama
export WORKBENCH_OLLAMA_MODEL="your-installed-tool-model"
unset WORKBENCH_OLLAMA_URL
python -m workbench --port 8765
```

浏览器仍打开 [http://localhost:8765](http://localhost:8765)。模型输出仍需通过服务端工具校验、权限和审批；界面的固定场景选择只控制离线规则，不能保证真实模型选择相同工具。若本地模型不可用、返回结构不正确或超出执行边界，应观察明确的失败或拒绝结果。

回到离线演示时，停止服务并执行前面的恢复命令。协议模拟测试不能视为某个真实模型已经通过效果评测。

## 认识身份

| 身份标识 | 观察目的 |
|---|---|
| `alpha-owner` | 同一演示租户的所有者操作与重置 |
| `alpha-member` | 成员的查询、申请写入与确认流程 |
| `alpha-viewer` | 只读身份的查询和写入拒绝 |
| `beta-owner` | 另一个租户的数据作用域 |

身份选择器供演示使用，不需要真实注册。任何人在本机都能切换这些预设身份，因此它不是真实认证机制。

## 建议演示顺序

### 1. 查询知识并查看来源

选择 `alpha-member`，输入以下内容：

```text
查询报销规则
```

也可先阅读界面列出的合成知识文档，再用文档中的关键词提问。

观察：

- 步骤面板是否展示计划、工具调用与结果。
- 回答是否关联实际返回的文档标题和片段。
- 未命中时是否如实说明，而非编造文档引用。

本步骤使用关键词检索。提问时沿用文档中的关键词比任意改写更容易命中，这也是 baseline 的局限。

### 2. 发起写入并确认

仍使用 `alpha-member`，输入以下内容：

```text
创建任务：准备报销材料
```

观察待审批状态与拟执行参数。确认前查看任务列表：不应因为提出请求就完成写入。确认后，再查看任务列表和执行步骤。

审批对象应保留原请求的身份、租户及参数。确认过程中切换身份或修改前端显示，不应改变服务端已保存的原操作。

### 3. 拒绝一次操作

再次提出创建任务的请求，在审批区选择拒绝。观察运行结果和任务列表，确认被拒绝的操作不产生任务。

### 4. 对比角色权限

切换到 `alpha-viewer`，提出写入请求。观察拒绝结果；仅将前端按钮隐藏不能代表已经完成权限检查，服务端也必须拒绝。

### 5. 对比租户作用域

切换到 `beta-owner`，查看该身份的文档、任务和可见审计记录。与此前租户比较，理解数据过滤发生在服务端。所有者能查看本租户审计事件，成员与只读身份只能查看自己的事件。

跨租户隔离的具体边界还应通过直接接口测试验证，不能只依赖界面上的差异。

### 6. 重置演示

使用某租户的所有者身份重置当前租户数据，或停止后重新启动服务。内存状态不会持久保存，重启恢复初始样例属于预期行为。

## 自动化验证

在项目根目录执行：

```bash
python -m unittest discover -s tests -v
```

实际验证环境、覆盖范围和结果见 [验证记录](verification.md)。测试通过只表示已覆盖用例在记录环境中通过，不能证明生产上线质量。

## 常见问题

**启动后没有自动打开网页。** 在浏览器手动输入 `http://localhost:8765`，终端仍需保持运行。

**端口被占用。** 可使用其他本机端口，例如 `python -m workbench --port 8766`，浏览器地址也同步改为 `http://localhost:8766`。不要为了运行演示而关闭系统安全控制。

**提示找不到 workbench 模块。** 检查终端当前目录是否为仓库根目录，应能看到 `workbench` 文件夹。

**问题没有命中知识。** 默认检索依赖关键词，先参考界面的样例文档标题和内容。这个现象不表示调用了真实模型后仍得出相同结果。

**重启后刚建的任务没有了。** 当前状态仅保存在内存中，重启会恢复初始数据。

**为什么没有真正的登录页？** 该演示重点是角色和租户的执行边界，固定身份用于复现测试。生产认证需要另行实现。

## 演示结束后

停止服务，保留代码、测试结果与自己的理解即可。不要把合成演示改成真实客户数据库后直接开放访问；生产化的待补项见 [架构说明](architecture.md)。
