from flask import Flask, request, render_template, redirect, jsonify, session, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import sqlite3
import re
import os

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'kakeibo-dev-key-change-me')
DB = "kakeibo.db"

_ocr_reader = None


def get_ocr_reader():
    global _ocr_reader
    if _ocr_reader is None:
        import easyocr
        _ocr_reader = easyocr.Reader(['ja', 'en'], gpu=False)
    return _ocr_reader


def get_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def safe_int(v):
    try:
        return int(float(str(v).strip() or 0))
    except Exception:
        return 0


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        expense_date TEXT,
        category_id INTEGER,
        amount INTEGER DEFAULT 0,
        memo TEXT DEFAULT '',
        user_id INTEGER DEFAULT 1
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS income (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        income_date TEXT,
        category_id INTEGER,
        amount INTEGER DEFAULT 0,
        memo TEXT DEFAULT '',
        user_id INTEGER DEFAULT 1
    )
    """)

    # categories テーブル
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='categories'")
    categories_exists = cur.fetchone() is not None

    if not categories_exists:
        cur.execute("""
        CREATE TABLE categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_name TEXT,
            category_name TEXT,
            user_id INTEGER DEFAULT 1
        )
        """)
    else:
        cur.execute("PRAGMA table_info(categories)")
        cat_cols = [r['name'] for r in cur.fetchall()]
        if 'user_id' not in cat_cols:
            cur.execute("ALTER TABLE categories RENAME TO categories_old")
            cur.execute("""
            CREATE TABLE categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_name TEXT,
                category_name TEXT,
                user_id INTEGER DEFAULT 1
            )
            """)
            try:
                cur.execute("""
                INSERT INTO categories (id, group_name, category_name, user_id)
                SELECT id, group_name, category_name, 1 FROM categories_old
                """)
                cur.execute("DROP TABLE categories_old")
            except Exception:
                pass

    # income_categories テーブル
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='income_categories'")
    income_categories_exists = cur.fetchone() is not None

    if not income_categories_exists:
        cur.execute("""
        CREATE TABLE income_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_name TEXT,
            user_id INTEGER DEFAULT 1
        )
        """)
    else:
        cur.execute("PRAGMA table_info(income_categories)")
        inc_cat_cols = [r['name'] for r in cur.fetchall()]
        if 'user_id' not in inc_cat_cols:
            cur.execute("ALTER TABLE income_categories RENAME TO income_categories_old")
            cur.execute("""
            CREATE TABLE income_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_name TEXT,
                user_id INTEGER DEFAULT 1
            )
            """)
            try:
                cur.execute("""
                INSERT INTO income_categories (id, category_name, user_id)
                SELECT id, category_name, 1 FROM income_categories_old
                """)
                cur.execute("DROP TABLE income_categories_old")
            except Exception:
                pass

    # 既存テーブルへのカラム追加（マイグレーション）
    for sql in [
        "ALTER TABLE expenses ADD COLUMN memo TEXT DEFAULT ''",
        "ALTER TABLE income ADD COLUMN memo TEXT DEFAULT ''",
        "ALTER TABLE expenses ADD COLUMN user_id INTEGER DEFAULT 1",
        "ALTER TABLE income ADD COLUMN user_id INTEGER DEFAULT 1",
    ]:
        try:
            cur.execute(sql)
        except Exception:
            pass

    conn.commit()
    conn.close()


DEFAULT_EXPENSE_CATEGORIES = [
    ('生活費', '食費'),
    ('生活費', '住宅'),
    ('生活費', '日用品'),
    ('交通', '交通費'),
    ('医療', '医療費'),
    ('通信', '通信費'),
    ('光熱費', '光熱費'),
    ('衣服・美容', '衣服'),
    ('娯楽', '娯楽'),
    ('交際', '交際費'),
]
DEFAULT_INCOME_CATEGORIES = ['給与', '副収入', 'その他']


def create_default_categories(user_id):
    conn = get_conn()
    cur = conn.cursor()
    for group, name in DEFAULT_EXPENSE_CATEGORIES:
        cur.execute(
            "INSERT INTO categories (group_name, category_name, user_id) VALUES (?, ?, ?)",
            (group, name, user_id)
        )
    for name in DEFAULT_INCOME_CATEGORIES:
        cur.execute(
            "INSERT INTO income_categories (category_name, user_id) VALUES (?, ?)",
            (name, user_id)
        )
    conn.commit()
    conn.close()


def create_icons():
    try:
        from PIL import Image, ImageDraw
        os.makedirs('static', exist_ok=True)
        for size in [192, 512]:
            path = f'static/icon-{size}.png'
            if not os.path.exists(path):
                img = Image.new('RGB', (size, size), color='#667eea')
                d = ImageDraw.Draw(img)
                m = size // 5
                d.ellipse([m, m, size - m, size - m], fill='white')
                fs = size // 3
                d.rectangle([size // 2 - fs // 6, m + size // 6,
                              size // 2 + fs // 6, size - m - size // 8], fill='#667eea')
                d.rectangle([size // 2 - fs // 2, size // 2 - fs // 8,
                              size // 2 + fs // 2, size // 2 + fs // 8], fill='#667eea')
                d.rectangle([size // 2 - fs // 3, size // 2 + fs // 8,
                              size // 2 + fs // 3, size // 2 + fs // 4], fill='#667eea')
                img.save(path)
    except Exception:
        pass


# ======================
# 認証
# ======================
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE username = ?", (username,))
        user = cur.fetchone()
        conn.close()
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('home'))
        error = "ユーザー名またはパスワードが違います"
    return render_template("login.html", error=error)


@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")
        if not username or not password:
            error = "ユーザー名とパスワードを入力してください"
        elif len(password) < 4:
            error = "パスワードは4文字以上にしてください"
        elif password != password2:
            error = "パスワードが一致しません"
        else:
            conn = get_conn()
            cur = conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                    (username, generate_password_hash(password))
                )
                conn.commit()
                user_id = cur.lastrowid
                conn.close()
                create_default_categories(user_id)
                session['user_id'] = user_id
                session['username'] = username
                return redirect(url_for('home'))
            except sqlite3.IntegrityError:
                conn.close()
                error = "そのユーザー名はすでに使われています"
    return render_template("register.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for('login'))


# ======================
# HOME
# ======================
@app.route("/", methods=["GET", "POST"])
@login_required
def home():
    conn = get_conn()
    cur = conn.cursor()
    uid = session['user_id']

    import datetime
    today_ym = datetime.date.today().strftime("%Y-%m")
    month = request.args.get("month", today_ym)
    if month == "all":
        month = ""

    if request.method == "POST":
        t = request.form.get("form_type")

        if t == "expense":
            cur.execute("""
                INSERT INTO expenses (expense_date, category_id, amount, memo, user_id)
                VALUES (?, ?, ?, ?, ?)
            """, (
                request.form.get("expense_date"),
                request.form.get("category"),
                safe_int(request.form.get("amount")),
                request.form.get("memo", ""),
                uid
            ))

        elif t == "income":
            cur.execute("""
                INSERT INTO income (income_date, category_id, amount, memo, user_id)
                VALUES (?, ?, ?, ?, ?)
            """, (
                request.form.get("income_date"),
                request.form.get("income_category"),
                safe_int(request.form.get("amount")),
                request.form.get("memo", ""),
                uid
            ))

        elif t == "category_add":
            cat_name = request.form.get("category_name", "").strip()
            if cat_name:
                cur.execute(
                    "SELECT id FROM categories WHERE category_name = ? AND user_id = ?",
                    (cat_name, uid)
                )
                if not cur.fetchone():
                    cur.execute(
                        "INSERT INTO categories (group_name, category_name, user_id) VALUES (?, ?, ?)",
                        (request.form.get("group_name"), cat_name, uid)
                    )

        elif t == "income_category_add":
            cat_name = request.form.get("category_name", "").strip()
            if cat_name:
                cur.execute(
                    "SELECT id FROM income_categories WHERE category_name = ? AND user_id = ?",
                    (cat_name, uid)
                )
                if not cur.fetchone():
                    cur.execute(
                        "INSERT INTO income_categories (category_name, user_id) VALUES (?, ?)",
                        (cat_name, uid)
                    )

        conn.commit()

    where_e = "WHERE e.user_id = ?"
    params_e = [uid]
    if month:
        where_e += " AND e.expense_date LIKE ?"
        params_e.append(month + "%")

    cur.execute(f"""
        SELECT e.id, e.expense_date, e.amount, e.memo, e.category_id, c.category_name
        FROM expenses e
        LEFT JOIN categories c ON e.category_id = c.id
        {where_e}
        ORDER BY e.expense_date DESC, e.id DESC
    """, params_e)
    expenses = cur.fetchall()

    where_i = "WHERE i.user_id = ?"
    params_i = [uid]
    if month:
        where_i += " AND i.income_date LIKE ?"
        params_i.append(month + "%")

    cur.execute(f"""
        SELECT i.id, i.income_date, i.amount, i.memo, i.category_id, c.category_name
        FROM income i
        LEFT JOIN income_categories c ON i.category_id = c.id
        {where_i}
        ORDER BY i.income_date DESC, i.id DESC
    """, params_i)
    incomes = cur.fetchall()

    cur.execute("SELECT * FROM categories WHERE user_id = ? ORDER BY group_name, category_name", (uid,))
    categories = cur.fetchall()

    cur.execute("SELECT * FROM income_categories WHERE user_id = ? ORDER BY category_name", (uid,))
    income_categories = cur.fetchall()

    expense_total = sum(safe_int(x["amount"]) for x in expenses)
    income_total = sum(safe_int(x["amount"]) for x in incomes)
    balance = income_total - expense_total

    cur.execute("SELECT COALESCE(SUM(amount),0) FROM income WHERE user_id = ?", (uid,))
    all_income = safe_int(cur.fetchone()[0])
    cur.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE user_id = ?", (uid,))
    all_expense = safe_int(cur.fetchone()[0])
    all_time_balance = all_income - all_expense

    chart_params = [uid]
    chart_where = "WHERE e.user_id = ?"
    if month:
        chart_where += " AND e.expense_date LIKE ?"
        chart_params.append(month + "%")

    cur.execute(f"""
        SELECT c.category_name, SUM(e.amount) AS total
        FROM expenses e
        LEFT JOIN categories c ON e.category_id = c.id
        {chart_where}
        GROUP BY c.category_name
    """, chart_params)
    chart = cur.fetchall()
    chart_labels = [x["category_name"] or "未分類" for x in chart]
    chart_values = [safe_int(x["total"]) for x in chart]
    if not chart_labels:
        chart_labels = ["データなし"]
        chart_values = [0]

    cur.execute("""
        SELECT substr(expense_date,1,7) AS m, SUM(amount) AS total
        FROM expenses WHERE user_id = ? GROUP BY m
    """, (uid,))
    expense_m = {r["m"]: safe_int(r["total"]) for r in cur.fetchall()}

    cur.execute("""
        SELECT substr(income_date,1,7) AS m, SUM(amount) AS total
        FROM income WHERE user_id = ? GROUP BY m
    """, (uid,))
    income_m = {r["m"]: safe_int(r["total"]) for r in cur.fetchall()}

    months_all = sorted(set(expense_m.keys()) | set(income_m.keys()))
    monthly_labels = months_all
    monthly_income = [income_m.get(m, 0) for m in months_all]
    monthly_expense = [expense_m.get(m, 0) for m in months_all]
    balance_labels = months_all
    balance_values = [income_m.get(m, 0) - expense_m.get(m, 0) for m in months_all]

    cur.execute("""
        SELECT DISTINCT substr(expense_date,1,7) AS m
        FROM expenses WHERE user_id = ? ORDER BY m DESC
    """, (uid,))
    months = [x["m"] for x in cur.fetchall() if x["m"]]
    if today_ym not in months:
        months.insert(0, today_ym)

    conn.close()

    return render_template(
        "index.html",
        expenses=expenses,
        incomes=incomes,
        categories=categories,
        income_categories=income_categories,
        expense_total=expense_total,
        income_total=income_total,
        balance=balance,
        all_time_balance=all_time_balance,
        months=months,
        selected_month=month,
        chart_labels=chart_labels,
        chart_values=chart_values,
        monthly_labels=monthly_labels,
        monthly_income=monthly_income,
        monthly_expense=monthly_expense,
        balance_labels=balance_labels,
        balance_values=balance_values,
        username=session.get('username', '')
    )


# ======================
# 削除
# ======================
@app.route("/delete/<int:id>")
@login_required
def delete(id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM expenses WHERE id = ? AND user_id = ?", (id, session['user_id']))
    conn.commit()
    conn.close()
    return redirect("/")


@app.route("/delete_income/<int:id>")
@login_required
def delete_income(id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM income WHERE id = ? AND user_id = ?", (id, session['user_id']))
    conn.commit()
    conn.close()
    return redirect("/")


@app.route("/delete_category/<int:id>")
@login_required
def delete_category(id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM categories WHERE id = ? AND user_id = ?", (id, session['user_id']))
    conn.commit()
    conn.close()
    return redirect("/")


@app.route("/delete_income_category/<int:id>")
@login_required
def delete_income_category(id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM income_categories WHERE id = ? AND user_id = ?", (id, session['user_id']))
    conn.commit()
    conn.close()
    return redirect("/")


# ======================
# 編集
# ======================
@app.route("/edit_expense/<int:id>", methods=["POST"])
@login_required
def edit_expense(id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE expenses SET expense_date=?, category_id=?, amount=?, memo=?
        WHERE id=? AND user_id=?
    """, (
        request.form.get("expense_date"),
        request.form.get("category"),
        safe_int(request.form.get("amount")),
        request.form.get("memo", ""),
        id, session['user_id']
    ))
    conn.commit()
    conn.close()
    return redirect("/")


@app.route("/edit_income/<int:id>", methods=["POST"])
@login_required
def edit_income(id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE income SET income_date=?, category_id=?, amount=?, memo=?
        WHERE id=? AND user_id=?
    """, (
        request.form.get("income_date"),
        request.form.get("income_category"),
        safe_int(request.form.get("amount")),
        request.form.get("memo", ""),
        id, session['user_id']
    ))
    conn.commit()
    conn.close()
    return redirect("/")


# ======================
# レシートOCR
# ======================
@app.route("/upload_receipt", methods=["POST"])
@login_required
def upload_receipt():
    if 'receipt' not in request.files:
        return jsonify({"error": "ファイルがありません"}), 400

    file = request.files['receipt']
    if not file or file.filename == '':
        return jsonify({"error": "ファイルが選択されていません"}), 400

    img_bytes = file.read()

    try:
        from PIL import Image, ImageOps
        import io
        img = Image.open(io.BytesIO(img_bytes))
        img = ImageOps.exif_transpose(img)
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        max_px = 1500
        if max(img.size) > max_px:
            ratio = max_px / max(img.size)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=85)
        img_bytes = buf.getvalue()
    except Exception:
        pass

    try:
        reader = get_ocr_reader()
        results = reader.readtext(img_bytes)
        texts = [r[1] for r in results]
        amount, date = parse_receipt(texts)

        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM categories WHERE user_id = ? ORDER BY group_name, category_name", (session['user_id'],))
        categories = cur.fetchall()
        conn.close()

        cat_id, detected_type = suggest_category(texts, categories)

        return jsonify({
            "amount": amount,
            "date": date,
            "suggested_category": cat_id,
            "detected_type": detected_type,
            "lines": texts[:30]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ======================
# OCR ユーティリティ
# ======================
def normalize_text(s):
    result = []
    for c in s:
        code = ord(c)
        if 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - 0xFEE0))
        elif c == '　':
            result.append(' ')
        else:
            result.append(c)
    return ''.join(result)


def fix_ocr_nums(s):
    s = re.sub(r'[判半](?=[0-9])', '¥', s)
    s = re.sub(r'([0-9])の', r'\g<1>0', s)
    s = re.sub(r'の([0-9])', r'0\g<1>', s)
    return s


def remove_non_price_patterns(s):
    s = re.sub(r'\d{2,4}[-－]\d{3,4}[-－]\d{4}', '', s)
    s = re.sub(r'No[A-Za-z]?[-\s]*[0-9]+', '', s, flags=re.IGNORECASE)
    s = re.sub(r'[A-Za-z][-－][0-9]{5,}', '', s)
    return s


def find_price_nums(text, min_v=50, max_v=99999):
    t = remove_non_price_patterns(text)
    nums = []

    def is_valid(v):
        return min_v <= v <= max_v and not (2000 <= v <= 2030)

    for m in re.findall(r'[¥\\￥]\s*([0-9]{2,5})', t):
        v = int(m)
        if is_valid(v):
            nums.append(v)
    for m in re.findall(r'([0-9]{1,3},[0-9]{3})', t):
        v = int(m.replace(',', ''))
        if is_valid(v):
            nums.append(v)
    for m in re.findall(r'(?<![0-9/\-\.])([0-9]{3,5})(?![0-9/\-\.])', t):
        v = int(m)
        if is_valid(v):
            nums.append(v)
    return nums


CATEGORY_RULES = [
    (['食', 'レストラン', 'カフェ', 'フード', 'スーパー', 'コンビニ',
      'バーガー', 'ハンバーガー', 'ラーメン', 'ピザ', '寿司', '弁当', '惣菜',
      'MOS', 'KFC', 'マック', 'ファミレス', 'イオン', 'ライフ', '西友',
      'セブン', 'ファミマ', 'ローソン', 'ドッグ', 'チリ', 'モス',
      'BURGER', 'CAFE', 'RESTAURANT', 'FOOD', 'MART', '外食', '飲食'],
     '食費'),
    (['電車', 'バス', 'タクシー', 'JR', '鉄道', '交通', '運賃', 'SUICA', 'PASMO'],
     '交通費'),
    (['電気', 'ガス', '水道', '電力', 'エネルギー'],
     '光熱費'),
    (['携帯', 'スマホ', 'NTT', 'ドコモ', 'ソフトバンク', 'AU', '通信',
      'インターネット', 'SOFTBANK', 'DOCOMO'],
     '通信費'),
    (['病院', '薬局', 'クリニック', '薬', 'ドラッグ', '医院', '調剤'],
     '医療費'),
    (['ユニクロ', 'GU', '衣料', '洋服', 'アパレル', '靴', 'UNIQLO', 'ZARA'],
     '衣服'),
    (['映画', 'ゲーム', 'カラオケ', 'ボウリング', 'ジム', '書籍', 'CINEMA'],
     '娯楽'),
    (['ホームセンター', 'ニトリ', 'ダイソー', '百均', 'マツキヨ', 'ウエルシア', 'ツルハ'],
     '日用品'),
    (['居酒屋', 'バー', '酒', 'ワイン', 'サワー', 'ビール'],
     '交際費'),
]


def suggest_category(texts, categories):
    full = normalize_text(' '.join(texts)).upper()
    detected_type = None
    for ocr_kws, type_name in CATEGORY_RULES:
        if any(kw.upper() in full for kw in ocr_kws):
            detected_type = type_name
            break
    if not detected_type:
        return None, None
    for cat in categories:
        cname = cat['category_name']
        if detected_type in cname or cname in detected_type:
            return cat['id'], detected_type
    important_chars = [c for c in detected_type if c not in ('費', '用', '品')]
    for cat in categories:
        cname = cat['category_name']
        if any(c in cname for c in important_chars):
            return cat['id'], detected_type
    return None, detected_type


def parse_receipt(texts):
    import datetime
    from collections import Counter

    norm  = [normalize_text(t) for t in texts]
    fixed = [fix_ocr_nums(t) for t in norm]
    full  = '\n'.join(fixed)

    amount = None
    date   = None

    total_kws = [
        '合計', '合 計', '税込合計', '税込小計', '小計',
        'お会計', 'お支払', '支払合計', '請求', 'ご請求',
        'TOTAL', 'Total', 'total', '税込', '合', '計',
    ]

    for i, text in enumerate(fixed):
        if any(kw in text for kw in total_kws):
            for t in fixed[i:i + 7]:
                nums = find_price_nums(t)
                if nums:
                    amount = max(nums)
                    break
            if amount:
                break

    if not amount:
        all_nums = find_price_nums(full)
        if all_nums:
            counter = Counter(all_nums)
            for val, _ in counter.most_common():
                if val >= 100:
                    amount = val
                    break
            if not amount:
                amount = max(all_nums)

    today = datetime.date.today()

    def valid_date(mo, d):
        return 1 <= mo <= 12 and 1 <= d <= 31

    for m in re.finditer(r'(20[0-9]{2})[年/\-\.](\d{1,2})[月/\-\.](\d{1,2})', full):
        yr, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if valid_date(mo, d):
            date = f"{yr}-{mo:02d}-{d:02d}"
            break

    if not date:
        for m in re.finditer(r'(?:令和|[Rr])\s*(\d{1,2})\s*[年\./]?\s*(\d{1,2})\s*[月\./]?\s*(\d{1,2})', full):
            yr = 2018 + int(m.group(1))
            mo, d = int(m.group(2)), int(m.group(3))
            if valid_date(mo, d):
                date = f"{yr}-{mo:02d}-{d:02d}"
                break

    if not date:
        for m in re.finditer(r'(\d{2})[/\.](\d{2})[/\.](\d{2})', full):
            yr, mo, d = int('20' + m.group(1)), int(m.group(2)), int(m.group(3))
            if valid_date(mo, d):
                date = f"{yr}-{mo:02d}-{d:02d}"
                break

    if not date:
        for m in re.finditer(r'(\d{1,2})[月/](\d{1,2})日?', full):
            mo, d = int(m.group(1)), int(m.group(2))
            if valid_date(mo, d):
                date = f"{today.year}-{mo:02d}-{d:02d}"
                break

    return amount, date


if __name__ == "__main__":
    init_db()
    create_icons()
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
