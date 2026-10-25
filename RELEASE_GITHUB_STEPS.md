# GitHub 建仓、推送和预发布步骤

本轮仅在本地验收、构建和提交。以下远端命令留给维护者手动执行。
使用本机已经核对过帮助参数的 Git 和 GitHub CLI（`gh`）；先安装并登录二者。

## 发布源与身份

`local-release-20261025/public-source/` 是从最终受检 sdist 提取的独立软件源码快照，
附带最终软件验收摘要、SHA256SUMS 和 RELEASE_PROVENANCE.json。
它没有当前候选仓库的 `.git` 或旧历史，也没有旧真实数据派生 notebook。
公开源范围为受检软件包；本机保留的完整历史/迁移审计不随它发布。

原候选最终完整提交 SHA、验收快照 SHA 与制品 SHA256 在
`local-release-20261025/RELEASE_PROVENANCE.json` 中分别记录。
公开仓库的新提交 SHA 将与本地候选提交不同，不应冒用后者作为公开 tag 目标。
GitHub 自动生成的 source archives 也不等同于已验收的上传 sdist。

源码包包含测试资源，但依赖原 Git 历史、私有参考 verifier 和仓库控制工具的验收
必须在本地完整候选仓库按已声明的独立软件合同运行；不能在公开源码快照中将缺失
历史材料冒充通过。该快照支持包构建、安装与完整合成 Demo，不带旧混合历史 CI。

## 1. 本地准备与哈希复核（PowerShell）

```powershell
$candidateRoot = 'D:\Code\FactorLab-independent-candidate'
$bundle = Join-Path $candidateRoot 'local-release-20261025'
$publicSource = Join-Path $bundle 'public-source'
$repository = 'YOUR_OWNER/FactorLab'  # 替换为个人或组织的真实名称
$tag = 'v0.1.0.dev0'
$provenance = Get-Content -LiteralPath (Join-Path $bundle 'RELEASE_PROVENANCE.json') -Raw | ConvertFrom-Json
foreach ($artifact in $provenance.artifacts) {
    $artifactPath = Join-Path (Join-Path $bundle 'dist') $artifact.file
    $actual = (Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $artifact.sha256) { throw "制品哈希不符: $($artifact.file)" }
}
if (Test-Path -LiteralPath (Join-Path $publicSource '.git')) { throw '请使用未初始化的公开快照目录' }
Set-Location -LiteralPath $publicSource
git init -b main
git config core.autocrlf false
git add --all
git diff --cached --stat
git commit -m 'Release FactorLab 0.1.0.dev0 research software preview'
$publicCommit = (git rev-parse HEAD).Trim()
git status --short
```

Git 提交需要已配置的真实维护者姓名/邮箱。不要从候选原仓库执行推送旧历史的命令。

## 2. 创建 GitHub 仓库并推送（手动执行）

```powershell
gh auth login
gh repo create $repository --public --source . --remote origin --description 'Offline factor research software preview; NO LIVE MONEY'
git push -u origin main
git tag -a $tag $publicCommit -m 'FactorLab 0.1.0.dev0 research software preview; NO LIVE MONEY'
git push origin $tag
```

若目标仓库已经存在且为空，用 `git remote add origin https://github.com/OWNER/FactorLab.git`
替换 `gh repo create`，OWNER 同样替换为真实名称。

## 3. 创建 GitHub 预发布并上传受检制品（手动执行）

```powershell
$wheel = Join-Path $bundle 'dist\factorlab-0.1.0.dev0-py3-none-any.whl'
$sdist = Join-Path $bundle 'dist\factorlab-0.1.0.dev0.tar.gz'
$sums = Join-Path $bundle 'dist\SHA256SUMS.txt'
$evidence = Join-Path $bundle 'RELEASE_PROVENANCE.json'
$acceptance = Join-Path $candidateRoot 'SOFTWARE_RELEASE_ACCEPTANCE.md'
$notes = Join-Path $bundle 'github-release-notes.md'
gh release create $tag $wheel $sdist $sums $evidence $acceptance --repo $repository --verify-tag --prerelease --latest=false --title 'FactorLab 0.1.0.dev0 — 研究软件测试版' --notes-file $notes
gh release view $tag --repo $repository
```

这里明确选择预发布，不生成 PyPI 上传命令。保持
**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**。
