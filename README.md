# eaa-active-users

[English version](README.en.md)

[Akamai Enterprise Application Access (EAA)](https://techdocs.akamai.com/eaa/docs/welcome-guide) のアプリケーションに、指定した期間内にアクセスしたユーザーの一覧を出力する CLI ツールです。アクセス棚卸し・ライセンス整理・定期的なユーザーインベントリに使えます。

> **免責**: これは個人プロジェクトであり、Akamai Technologies が提供・承認・サポートするものではありません。現状有姿（as-is）・無保証・ベストエフォート（SLA なし）で提供します。

## なぜ作ったか

EAA はユーザーアクセスログを365日保持しており、{OPEN} API の
`GET /crux/v1/mgmt-pop/application-reports/ops/query` で照会できます。ところがこの API は **1回のコールで返すレコード数を黙って頭打ちにします**——[API リファレンス](https://techdocs.akamai.com/eaa-api/reference/get-application-reports)上の `limit` 最大値は 250、実際に観測される実効上限は 500（2026年8月時点）。上限に達してもエラーも切り捨てフラグも返りません。

そのため、長い期間を1回のクエリで取ろうとするクライアントは**気づかないままユーザーを取りこぼします**（公式 [cli-eaa](https://github.com/akamai/cli-eaa) の `report last_access` も v0.7.x 時点でこの影響を受けます。レスポンスが5,000件に達したときだけ期間を分割する実装ですが、サーバー側の上限がそれより低いため分割が発動しません）。

このツールは：

- どのサブ期間も上限未満に収まるまで**期間を再帰的に分割**するので、レコードが黙って落ちません
- 想定上限を**実行時に自動検証**し（1回だけ検証分割を行う）、実効上限がより低ければ自動補正します
- データが本当に取得不能なケース（1分間に上限以上のレコードが詰まっている場合）では、**完全性を装わず警告＋終了コード `3`** で明示します
- API レート制限（25リクエスト/分）を守り、429・ネットワークエラーはリトライします

## インストール

前提は **Python 3.10 以上**だけです。リポジトリを clone する必要はありません。

### 方法1: pipx（推奨・clone 不要）

[pipx](https://pipx.pypa.io/) はツールごとに隔離された環境を作ってくれるので、依存関係で手元の Python を汚しません。

```console
# pipx が無ければ（macOS の例）
brew install pipx
pipx ensurepath

# GitHub から直接インストール
pipx install git+https://github.com/isss802/eaa-active-users
```

これで `eaa-active-users` コマンドがそのまま使えます。更新は `pipx upgrade eaa-active-users`、削除は `pipx uninstall eaa-active-users`。

> このリポジトリがプライベートの間は、GitHub の認証（`gh auth login` 済み、または git の credential helper に GitHub の資格情報がある状態）が必要です。public なら認証不要でそのまま入ります。

### 方法2: pip（clone 不要）

venv を自分で管理したい場合：

```console
python3 -m venv ~/venvs/eaa
~/venvs/eaa/bin/pip install git+https://github.com/isss802/eaa-active-users
~/venvs/eaa/bin/eaa-active-users --help
```

### 方法3: uv（clone 不要・インストールすら不要）

[uv](https://docs.astral.sh/uv/) を使っているなら、一時実行が一番手軽です：

```console
uvx --from git+https://github.com/isss802/eaa-active-users eaa-active-users --help
```

### 方法4: clone して開発モード（開発者向け）

```console
git clone https://github.com/isss802/eaa-active-users
cd eaa-active-users
python3 -m venv .venv
.venv/bin/pip install -e . pytest ruff
.venv/bin/pytest   # テスト実行
```

> **注**: PyPI には公開していないため、`pip install eaa-active-users` / `pipx install eaa-active-users`（レジストリ名指定）はまだ使えません。

## 認証情報の準備

[Akamai Control Center](https://control.akamai.com/) の **Identity and Access Management** で API クライアントを作成し、サービス「**Enterprise Application Access**」に READ-WRITE でアクセスできるようにします。発行されたクレデンシャルを、EAA の契約 ID（`contract_id`）と一緒に `~/.edgerc` に書きます：

```ini
[default]
host = akab-xxxxxxxxxxxxxxxx-xxxxxxxxxxxxxxxx.luna.akamaiapis.net
client_token = akab-xxxxxxxxxxxxxxxx-xxxxxxxxxxxxxxxx
client_secret = xxxx
access_token = akab-xxxxxxxxxxxxxxxx-xxxxxxxxxxxxxxxx
contract_id = A-XXXXXXX
```

- `contract_id` は Enterprise Center の画面、または [Contracts API](https://techdocs.akamai.com/eaa-api/reference/get-contracts) で確認できます。
- 複数テナントを扱う場合はセクションを分けて `--section セクション名` で切り替えます。

## 使い方

```console
# 直近90日（デフォルト）のアクティブユーザーを CSV で標準出力へ
eaa-active-users

# 直近30日、JSON 形式、.edgerc の別セクションを使用
eaa-active-users --days 30 --format json --section my-tenant

# 期間を明示し、特定アプリ/IdP に絞り、ファイルに保存
eaa-active-users --start 2026-05-01T00:00:00Z --end 2026-08-01T00:00:00Z \
    --app login.example.com -o active-users.csv
```

出力（CSV）：

```csv
userid,access_count,first_access,last_access
alice@example.com,128,2026-05-03T10:00:00Z,2026-07-30T15:30:00Z
bob,4,2026-06-15T08:00:00Z,2026-06-18T12:00:00Z
```

`--tz Asia/Tokyo` を付けると日時が日本時間（オフセット付き）で出ます：

```csv
userid,access_count,first_access,last_access
alice@example.com,128,2026-05-03T19:00:00+09:00,2026-07-31T00:30:00+09:00
```

### 出力項目の説明

| 列 | 意味 |
|---|---|
| `userid` | ユーザーの識別子（API の `uid`）。通常はメールアドレスですが、メール形式でない値（Cloud Directory のユーザー名など）も返ります |
| `access_count` | 指定期間内にそのユーザーのアクセスログが記録された件数。**IdP ログインとアプリアクセスの両方**がイベントとして数えられます（HTTP リクエスト数とは一致しません）。活動量の目安 |
| `first_access` | **指定期間内で**最初にアクセスした日時 |
| `last_access` | **指定期間内で**最後にアクセスした日時（棚卸しの本命。「最後に使ったのはいつか」） |

- 日時は ISO 8601 形式です。デフォルトは UTC（末尾 `Z`）、`--tz` を指定するとそのタイムゾーンのオフセット付き（例 `+09:00`）で表示されます。
- 未認証アクセス（`anon-user`：インターネットからのスキャンやヘルスチェック）は**デフォルトで除外**され、除外件数が stderr に表示されます。含めたい場合は `--include-anonymous`。

## 未使用ユーザーの棚卸し（`--report unused`）

アクティブ一覧の「逆」——**ディレクトリに登録されているのに期間内にアクセスが無いユーザー**を洗い出します。テナントの全ディレクトリ（Cloud Directory / Active Directory / LDAP すべて）を列挙し、全登録ユーザーをアクティブ一覧と突合します。

```console
eaa-active-users --report unused --days 90 --tz Asia/Tokyo -o unused-review.csv
```

出力は登録ユーザー全員＋突合結果の一覧で、`verdict` 列でフィルタして使います：

| verdict | 意味 |
|---|---|
| `unused_candidate` | 期間内に EAA アプリアクセスの記録が無い＝**削除候補（ただし証明ではない。下記の注意参照）** |
| `needs_review` | 弱い一致（username がアクティブ側メールアドレスのローカル部と一致）のみ。**人が判断する行** |
| `active` | アクティブ一覧とフィールドが完全一致（`match_confidence` 列に `exact:email` 等の一致根拠） |
| `active_unmatched` | アクティブ一覧にいるのに、どのディレクトリユーザーとも一致しなかった ID（削除済みユーザーや表記ゆれの検知用） |

突合は `username` / `email` に加え、AD 連携で入る正規化属性（`user.email`、`user.userPrincipleName`、`user.samAccountName` 等）も大文字小文字を無視して照合します。

> **⚠️ `unused_candidate` を機械的に削除しないでください。** この判定は「期間内に EAA のアクセスログに記録が無い」ことを意味します。
>
> なお「IdP（ログインポータル）にログインしただけの利用者が漏れるのではないか」という懸念は**実測で否定済み**です——IdP ログインイベントもこのレポートに記録されることを、期間中ログインのみだった実ユーザーがレコード件数・時刻とも完全一致で出現することにより確認しています（2026-08、複数ディレクトリ構成のテナントで検証。あわせて Web アプリ／カスタムドメイン／トンネル型クライアントアクセスの記録も実測確認済み）。
>
> 残る注意は2点です：①アプリ種別ごとの記録範囲は公式ドキュメント上は網羅保証の明記がない（上記のとおり主要種別は実測で確認済み）、②名寄せに失敗した実利用者が紛れうる（その検知用が `needs_review` と `active_unmatched` です）。独立した裏取りが要る場合は公式 CLI の生ログと突き合わせできます：`akamai eaa log access -s <epoch> -e <epoch> --json | jq -r 'select(.username != "-") | .username' | sort -u`（要 [cli-eaa](https://github.com/akamai/cli-eaa) と Legacy API キー）。最終判断は必ず人が行ってください。

### 主なオプション

| オプション | 説明 |
|---|---|
| `--report active\|unused` | active＝アクティブユーザー一覧（デフォルト）／unused＝ディレクトリ全ユーザーとの突合（未使用候補の洗い出し） |
| `--days N` | 遡る日数（デフォルト 90。`--start` 指定時は無視） |
| `--start` / `--end` | 期間の明示指定（epoch 秒 or ISO 8601。`--end` 省略時は現在） |
| `--section` | `~/.edgerc` のセクション（デフォルト `default`） |
| `--edgerc` | `.edgerc` のパス（デフォルト `~/.edgerc`） |
| `--app` | アプリケーション/IdP のホスト名または UUID で絞り込み |
| `--tz` | タイムゾーン（例 `Asia/Tokyo`）。API クエリと**出力日時の表示**の両方に使う（デフォルト UTC） |
| `--format csv\|json` | 出力形式（デフォルト csv） |
| `-o ファイル` | ファイルへ出力（デフォルトは標準出力） |
| `--include-anonymous` | `anon-user`（未認証アクセス）を出力に含める |
| `--cap N` | 分割判定に使う1コールあたり上限の想定値（デフォルト 250。低いほど安全・高いほど速い） |
| `--verbose` | 全ウィンドウの取得ログを stderr に出す |

### 終了コード

| コード | 意味 |
|---|---|
| `0` | 成功（完全なデータ） |
| `1` | 使い方・認証情報のエラー |
| `2` | データ取得前の API エラー |
| `3` | **完了したが不完全**（下記「制限事項」参照。stderr に警告が出ます） |

## 制限事項

- **1コールあたりのレコード上限は非文書化の挙動です。** デフォルトの想定値（`--cap 250`）はドキュメント記載の `limit` 最大値に合わせており実行時に検証もされますが、Akamai がこの挙動をいつ変えてもおかしくありません。
- 1分間（最小分割幅）に上限以上のレコードが詰まっている場合、その1分の超過分はこのエンドポイントからは取得できません。ツールは完全なふりをせず、警告と終了コード `3` で知らせます。
- EAA のログ保持は365日です。それより古い期間は何も返りません。
- 出力には個人データ（ユーザー名）が含まれます。結果ファイルの取り扱いに注意してください。
- API レート制限は25リクエスト/分です。大規模テナント×長期間は時間がかかります（ツール側は約24リクエスト/分に自制します）。

## 開発

```console
uv sync           # または: pip install -e . --group dev
pytest
ruff check .
```

## 謝辞

期間分割のアプローチは [cli-eaa](https://github.com/akamai/cli-eaa)（Apache-2.0）のソースを読んで着想を得ました。cli-eaa からのコードのコピーは含まれていません。

## メンテナンス方針

ベストエフォート・SLA なしです。上流で API の挙動が修正・文書化された場合（または cli-eaa の `report last_access` が上限を正しく扱うようになった場合）、このリポジトリは公式ツール推奨に切り替えて deprecated → アーカイブします。

## ライセンス

[MIT](LICENSE)
