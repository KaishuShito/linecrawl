# linecrawl

`linecrawl` は、LINE のトーク履歴を自分のパソコンに保存して、あとから検索できるようにするコマンドラインツールです。

できることは主に3つです。

- LINE Desktop の「トークを保存」で出したテキストを取り込む
- Chrome の LINE Web 拡張で開いているトークを取り込む
- 取り込んだメッセージや画像・スタンプを検索する

LINE のパスワードやログイン情報は保存しません。保存先は自分のパソコン内のデータベースと画像フォルダだけです。

## まずやること

インストールしたら、まずこの2つを実行してください。

```bash
linecrawl --help
linecrawl --json doctor --explain
```

`doctor` は、保存先、メッセージ件数、画像の保存状況、自動取り込みの状態を確認するコマンドです。

`--json` はサブコマンドの前に置きます。

```bash
linecrawl --json doctor
linecrawl --json search "相談"
```

いつもの保存先を汚さず試したいときは、一時的な保存先を使います。

```bash
linecrawl --db /tmp/linecrawl-test.db --json doctor --explain
```

## インストール

```bash
git clone https://github.com/KaishuShito/linecrawl.git
cd linecrawl
make install-local
```

これで `linecrawl` コマンドが使えるようになります。

```bash
command -v linecrawl
linecrawl --help
```

## どの方法で取り込むか

目的に合わせて選びます。

- テキストだけでよい: LINE Desktop の「トークを保存」
- 画像やスタンプもほしい: Chrome の LINE Web 拡張
- 全トークの履歴をまとめて取り込みたい: `web-import-all`
- すでに保存した LINE Web のJSONファイルを使いたい: `web-import-json`
- LINE の `.edb` ファイルを調べたい: 実験用。ふつうは使いません。

標準の保存先はここです。

```text
~/.linecrawl/linecrawl.db
~/.linecrawl/media/
```

## LINE Desktop から取り込む

テキストだけでよい場合は、この方法が一番シンプルです。

LINE Desktop で対象トークを開いてから実行します。

```bash
linecrawl desktop-save-current --import
```

うまくいかないときは、まずクリック位置だけ確認します。

```bash
linecrawl desktop-save-current --dry-run
```

すでに `[LINE]...txt` を保存してある場合は、Downloads からまとめて取り込めます。

```bash
linecrawl --json import-downloads
```

ファイルを指定して取り込むこともできます。

```bash
linecrawl --json import ~/Downloads/'[LINE]Family.txt'
```

## LINE Web から取り込む

画像やスタンプも取り込みたい場合は、LINE Web を使います。

1. Chrome で LINE Web 拡張を開く
2. 取り込みたいトークを開く
3. まず診断する

```bash
linecrawl --json web-doctor
```

問題なければ取り込みます。

```bash
linecrawl --json web-import-current --scroll-steps 5
```

画像・スタンプは標準で保存されます。画像がいらない場合:

```bash
linecrawl --json web-import-current --no-media
```

高解像度の画像がほしい場合だけ、`--full-media` を使います。

```bash
linecrawl --json web-import-current --method cdp --full-media
```

`--full-media` は、LINE Web の画像ビューアをページ内で一瞬開いて画像を保存します。ふつうの取り込みでは不要です。

## 全トークをまとめて取り込む

`web-import-current` は「いま開いているトーク」だけを取り込みます。サイドバーの
全トークを扱いたい場合は `web-chats` と `web-import-all` を使います。

まずサイドバーの全トークを一覧します（取り込みはしません）。これは確実に動きます。

```bash
linecrawl --json web-chats
```

すべてのトークを順に開いて取り込みます。

```bash
linecrawl --json web-import-all --scroll-steps 5
```

`web-import-all` は LINE Web のハッシュルーター（`#/chats/<id>`）でトークを
1つずつ切り替えながら取り込み、終わったら元のトークに戻します。OSレベルの
クリックやタブ切り替えはしませんが、LINE タブの表示は実行中に切り替わるので、
LINE タブを操作しながらの実行は避けてください。

**重要な制約**: LINE Web はエンドツーエンド暗号化で、メッセージをローカルに
保存しません（IndexedDB には公開鍵しか無い）。そのため、トークの本文は「実際に
そのトークを開いて描画された時」だけ DOM に現れます。プログラムからのトーク
切り替えではルートは変わっても最新1件しか描画されないことが多く、深い履歴を
確実に取れるのは、いまの LINE セッションですでに開いたことのあるトークだけです。
特定の相手の全履歴がほしい場合は、そのトークを自分で開いてから
`web-import-current` を使うか、LINE Desktop の「トークを保存」を使ってください。
`web-import-all` 実行中は LINE タブをフォアグラウンドにしておくと、履歴が
読み込まれる可能性が上がります。

未読バッジのあるトークは、開くと相手に既読が付く可能性があるため、標準では
スキップして報告だけします。既読が付いてよければ opt-in します。

```bash
linecrawl --json web-import-all --include-unread
```

よく使うオプション:

```bash
linecrawl --json web-import-all --chat 'Family' --scroll-steps 20  # 名前で絞る
linecrawl --json web-import-all --limit 5 --no-media               # まず小さく試す
```

`--scroll-steps` はトークごとに何画面ぶん過去へ遡るかです。深い履歴が
ほしいトークは `--chat` で絞って大きめの値を指定するのが速いです。

## 最近の内容を取り込む

最近のLINE内容を調べる前に、まず状態を見ます。

```bash
linecrawl --json doctor --explain
```

LINE Web の状態を見る:

```bash
linecrawl --json web-doctor
```

いま開いている LINE Web トークを取り込む:

```bash
linecrawl --json web-import-current --scroll-steps 5 --force
```

Downloads にある Save Chat ファイルを取り込む:

```bash
linecrawl --json import-downloads
```

`locked` や `busy` と出たら、別の `linecrawl` が動いていないか確認します。

```bash
pgrep -af linecrawl
```

## 検索する

よく使う流れです。

1. まず `doctor` で状態を見る
2. 必要なら取り込む
3. `search` で探す
4. `messages` で前後を見る
5. 画像は `media` で見る

よく使うコマンド:

```bash
linecrawl --json search "相談"
linecrawl --json messages --chat "%Family%" --days 7 --limit 30
linecrawl --json media --chat "%Family%" --limit 10
linecrawl chats
```

画像・スタンプの最近分:

```bash
linecrawl --json media --limit 10
```

## 自動取り込み

ずっと取り込み続けたい場合だけ、自動取り込みを使います。

LINE Desktop の Save Chat ファイルを見張る:

```bash
linecrawl launchd-install --interval 10 --verbose
linecrawl launchd-status --label com.linecrawl.watch
linecrawl launchd-uninstall --label com.linecrawl.watch
```

LINE Web の開いているトークを見張る:

```bash
linecrawl launchd-install-web --interval 30 --scroll-steps 1
linecrawl launchd-status --label com.linecrawl.webwatch
linecrawl launchd-uninstall --label com.linecrawl.webwatch
```

LINE Web の自動取り込みは、Chrome で今開いているトークだけを取り込みます。別のトークを取り込みたい場合は、人間が Chrome でそのトークを開いてください。

一度だけ試す場合:

```bash
linecrawl web-watch-current --once --verbose
linecrawl watch --interval 10 --verbose
```

## LINE Web を使うときの注意

LINE Web 取り込みは、すでにログインしている Chrome を使います。`linecrawl` が LINE のパスワードやトークンを保存することはありません。

使える方法は主に2つです。

- CDP: Chrome を `--remote-debugging-port=9222` で起動する方法
- AppleScript: Chrome の `Allow JavaScript from Apple Events` を使う方法

CDP を使う場合:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222
```

Chrome 136+ では、いつもの Chrome プロファイルで CDP が使えないことがあります。その場合は AppleScript を使うか、専用プロファイルで Chrome を起動してください。

AppleScript を使う場合は、Chrome でこれを有効にします。

```text
Chrome > View > Developer > Allow JavaScript from Apple Events
```

ふつうの LINE Web 取り込みでは、OS のマウス移動、キーボード入力、タブ切り替え、ウィンドウ移動はしません。例外は2つだけで、どちらもページ内 JavaScript で完結します: `--full-media`（画像ビューアを一瞬開く）と `web-import-all`（トークを順に切り替えて最後に元へ戻す）。

## 画像・スタンプ

LINE Web 取り込みでは、表示中の画像・スタンプを標準で保存します。

```bash
linecrawl --json web-import-current --scroll-steps 5
linecrawl --json media --limit 10
```

画像を保存しない:

```bash
linecrawl --json web-import-current --no-media
```

高解像度で保存する:

```bash
linecrawl --json web-import-current --method cdp --full-media
```

保存先はここです。

```text
~/.linecrawl/media/<chat>/<sha>.<ext>
```

メッセージと画像を一緒に見る:

```bash
linecrawl --json messages --chat "%Family%" --limit 10
```

画像まわりの状態を見る:

```bash
linecrawl --json doctor --explain
```

よく見る項目:

- `media`: 保存した画像・スタンプの数
- `media_full`: 高解像度で保存した数
- `media_dir`: 画像の保存先
- `media_files_missing`: 保存先には記録があるが、画像ファイルが見つからない数

## 機械で読みやすい出力(JSON)

自動処理では `--json` を使います。

```bash
linecrawl --json doctor
linecrawl --json search "相談"
linecrawl --json messages --limit 20
```

成功時は `ok: true` が返ります。

```json
{
  "ok": true,
  "messages": []
}
```

失敗時は `ok: false` と `error` が返ります。

```json
{
  "ok": false,
  "error": {
    "code": "no_files_matched",
    "message": "No files matched."
  }
}
```

## 詳しく調べる(SQL)

件数や日付範囲を正確に見たいときは `sql` を使います。

`sql` は読み取り専用です。保存先を書き換えるSQLは実行できません。

```bash
linecrawl sql "select count(*) as messages from messages;" --json
linecrawl sql "select name from chats order by name;"
linecrawl sql "select path, quality from media order by captured_at desc limit 5;" --json
```

## `.edb` について

LINE Desktop の `.edb` 直接取り込みは実験中です。ふつうは Save Chat か LINE Web を使ってください。

診断だけする:

```bash
linecrawl --json edb-doctor
```

書き込まずに試す:

```bash
linecrawl --json edb-import --dry-run
```

`unsupported-encrypted-or-wrapped` が返る場合があります。これは、LINE の `.edb` が暗号化または独自形式で保存されているという意味で、現時点では正常です。

## 安全に使うために

`linecrawl` は LINE 公式ツールではありません。LINE Corporation、LY Corporation、NAVER とは関係ありません。

- 自分が読めるトークだけを扱ってください。
- 他人のメッセージを含むデータは慎重に扱ってください。
- LINE の利用規約や法律に従ってください。
- LINE Web や `.edb` まわりは、LINE 側の変更で動かなくなることがあります。
- このソフトウェアは無保証です。詳しくは [LICENSE](LICENSE) を見てください。

## 開発するとき

このリポジトリを変更したら、テストを実行します。

```bash
make test
python3 -m unittest discover -s tests -v
linecrawl --help
linecrawl --json doctor --explain
```

インストール済みの `linecrawl` に新しい機能が見えない場合は、先にローカルの `linecrawl.py` を直接実行して確認してください。

```bash
python3 linecrawl.py --help
```

help を変えたときは、全サブコマンドの `--help` がエラーなく表示されることも確認してください。

## ライセンス

MIT — see [LICENSE](LICENSE).
