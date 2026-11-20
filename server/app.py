from flask import Flask, redirect, request, jsonify, render_template, url_for, session, flash, Response, current_app
from flask_apscheduler import APScheduler
from flask_mail import Mail, Message
from werkzeug.utils import secure_filename

try:
    from server.models import db, User, AffiliateLink, ClickEvent, ScheduledPost, Notification, Sponsorship
except ImportError:
    from models import db, User, AffiliateLink, ClickEvent, ScheduledPost, Notification, Sponsorship
from werkzeug.security import generate_password_hash, check_password_hash
from groq import Groq
import csv
import datetime
import io
import os
import uuid


ALLOWED_AVATAR_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}


def allowed_avatar_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_AVATAR_EXTENSIONS


class Config:
    SCHEDULER_API_ENABLED = True


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config())
    app.secret_key = os.getenv('SECRET_KEY', 'secret123')

    raw_db_url = (
        os.getenv("DATABASE_URL")
        or os.getenv("MYSQL_PRIVATE_URL")
        or os.getenv("MYSQL_URL")
    )

    if not raw_db_url:
        raise Exception("No database URL found")

    db_url = raw_db_url.strip()

    # Railway may provide mysql://
    if db_url.startswith("mysql://"):
        db_url = db_url.replace(
            "mysql://",
            "mysql+pymysql://",
            1
        )

    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # Startup Debug Logging: Print safe connection string with password masked
    safe_log_url = db_url
    if '@' in safe_log_url and '://' in safe_log_url:
        prefix, remainder = safe_log_url.split('://', 1)
        credentials, host_part = remainder.split('@', 1)
        if ':' in credentials:
            user = credentials.split(':', 1)[0]
            safe_log_url = f"{prefix}://{user}:******@{host_part}"
    print(f" [DB CONFIG] Active SQLAlchemy Database URI: {safe_log_url}")

    # Configure Flask-Mail (Using Gmail SMTP)
    app.config['MAIL_SERVER'] = 'smtp.googlemail.com'
    app.config['MAIL_PORT'] = 587
    app.config['MAIL_USE_TLS'] = True
    app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME', 'createropshub@gmail.com')
    app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD', 'mlij medl nptm rrxb')
    app.config['MAIL_DEFAULT_SENDER'] = ('CreatorOps Hub', 'createropshub@gmail.com')

    # Initialize extensions
    db.init_app(app)
    mail = Mail(app)

    # Automatically create all tables on startup for production/Railway safely
    with app.app_context():
        try:
            db.create_all()
            os.makedirs(os.path.join(app.root_path, "static", "uploads", "avatars"), exist_ok=True)
            print(" [DB NOTICE] Database tables verified/created successfully.")
        except Exception as e:
            print(f" [DB ERROR] Could not create tables: {e}")

    # Initialize Groq Client safely
    groq_client = None
    try:
        groq_client = Groq(api_key=os.getenv('GROQ_API_KEY'))
    except Exception as e:
        print(f" [GROQ NOTICE] Groq client initialization warning: {e}")

    # Initialize APScheduler
    scheduler = APScheduler()
    scheduler.init_app(app)
    scheduler.start()

    # Context processor with safe error handling to prevent 500 rollbacks
    @app.context_processor
    def inject_notifications():
        user_notifications = []
        current_avatar = None
        if 'user_id' in session:
            try:
                user_id = session.get('user_id')
                db_notifications = Notification.query.filter_by(user_id=user_id).order_by(Notification.id.desc()).limit(5).all()
                user_notifications = [
                    {"title": n.title, "message": n.message} for n in db_notifications
                ]
                user_obj = db.session.get(User, user_id)
                if user_obj:
                    current_avatar = getattr(user_obj, "avatar_url", None)
            except Exception as e:
                db.session.rollback()
                print(f" [NOTIF ERROR] {e}")
        return dict(notifications=user_notifications, current_avatar=current_avatar)

    # Global Error Handlers for robust transaction rollbacks
    @app.errorhandler(500)
    def internal_error(error):
        db.session.rollback()
        return "An internal server error occurred. Database transaction rolled back.", 500

    # Upgraded Background Task: Auto-Publishing Simulation + In-App Alerts + Email
    @scheduler.task(
        'interval',
        id='publish_scheduled_posts',
        seconds=60,
        misfire_grace_time=900
    )
    def background_publish_job():

        with app.app_context():

            now = datetime.datetime.utcnow()

            print("BACKGROUND JOB RUNNING", flush=True)
            print("CURRENT UTC TIME:", now, flush=True)

            due_posts = ScheduledPost.query.filter(
                ScheduledPost.status == 'scheduled',
                ScheduledPost.scheduled_for <= now
            ).all()

            print("FOUND POSTS:", len(due_posts), flush=True)

            for post in due_posts:

                print("PROCESSING POST:", post.title, flush=True)

                try:
                    post.status = 'published'

                    user = db.session.get(User, post.user_id)

                    if user:
                        print(
                            f"CREATING NOTIFICATION FOR USER: {user.username}",
                            flush=True
                        )

                        new_notif = Notification(
                            user_id=user.id,
                            title=f"Auto-Published: {post.platform}",
                            message=f"Post '{post.title}' was automatically pushed live via API."
                        )

                        db.session.add(new_notif)

                    db.session.commit()

                    print(
                        f"POST PUBLISHED: {post.title}",
                        flush=True
                    )

                except Exception as e:

                    db.session.rollback()

                    print(
                        f"SCHEDULER ERROR: {e}",
                        flush=True
                    )

    @app.route('/test-publish')
    def test_publish():

        now = datetime.datetime.now()

        due_posts = ScheduledPost.query.filter(
            ScheduledPost.status == 'scheduled',
            ScheduledPost.scheduled_for <= now
        ).all()

        for post in due_posts:
            post.status = 'published'

            user = db.session.get(User, post.user_id)

            if user:
                db.session.add(
                    Notification(
                        user_id=user.id,
                        title=f"Auto-Published: {post.platform}",
                        message=f"Post '{post.title}' was automatically pushed live via API."
                    )
                )

        db.session.commit()

        return f"Processed {len(due_posts)} posts"

    @app.route('/')
    def home():
        if 'user_id' not in session:
            return redirect(url_for('login_page'))

        user_id = session.get('user_id')
        total_clicks = (
            db.session.query(db.func.sum(AffiliateLink.click_count))
            .filter_by(user_id=user_id)
            .scalar()
            or 0
        )
        active_links_count = AffiliateLink.query.filter_by(user_id=user_id).count()
        top_link = (
            AffiliateLink.query.filter_by(user_id=user_id)
            .order_by(AffiliateLink.click_count.desc())
            .first()
        )
        recent_links = (
            AffiliateLink.query.filter_by(user_id=user_id)
            .order_by(AffiliateLink.id.desc())
            .limit(5)
            .all()
        )

        return render_template(
            'dashboard.html',
            total_clicks=total_clicks,
            active_links_count=active_links_count,
            top_link=top_link,
            recent_links=recent_links,
        )

    @app.route('/server-time')
    def server_time():
        return str(datetime.datetime.now())

    @app.route('/login', methods=['GET', 'POST'])
    def login_page():
        if request.method == 'POST':
            identifier = request.form.get('username')
            password = request.form.get('password')

            try:
                user = User.query.filter((User.username == identifier) | (User.email == identifier)).first()
                if user and check_password_hash(user.password_hash, password):
                    session['user_id'] = user.id
                    session['username'] = user.username
                    return redirect(url_for('home'))
            except Exception as e:
                db.session.rollback()
                print(f" [LOGIN ERROR] {e}")
                return render_template('login.html', error="Database error occurred. Please try again.")

            return render_template('login.html', error="Invalid username or password")

        return render_template('login.html')

    @app.route('/signup', methods=['GET', 'POST'])
    def signup_page():
        if request.method == 'POST':
            username = request.form.get('username')
            email = request.form.get('email')
            password = request.form.get('password')

            if not username or not email or not password:
                return render_template('signup.html', error="All fields are required")

            try:
                if User.query.filter_by(email=email).first():
                    return render_template('signup.html', error="Email already registered")

                hashed_password = generate_password_hash(password)
                new_user = User(
                    username=username,
                    email=email,
                    password_hash=hashed_password,
                    email_notifications=False,
                )

                db.session.add(new_user)
                db.session.commit()

                return redirect(url_for('login_page'))
            except Exception as e:
                db.session.rollback()
                print(f" [SIGNUP ERROR] {e}")
                return render_template('signup.html', error="Database error occurred during registration.")

        return render_template('signup.html')

    @app.route('/forgot-password', methods=['GET', 'POST'])
    def forgot_password_page():
        if request.method == 'POST':
            email = request.form.get('email')
            new_password = request.form.get('new_password')
            try:
                user = User.query.filter_by(email=email).first()
                if user:
                    if new_password:
                        user.password_hash = generate_password_hash(new_password)
                        db.session.commit()
                        return render_template(
                            'forgot_password.html',
                            message="Password successfully reset. You can now login.",
                        )
                    return render_template('forgot_password.html', error="New password is required.")
                return render_template('forgot_password.html', error="Email address not found.")
            except Exception as e:
                db.session.rollback()
                print(f" [FORGOT PASSWORD ERROR] {e}")
                return render_template('forgot_password.html', error="Database error occurred.")
        return render_template('forgot_password.html')

    @app.route('/links', methods=['GET', 'POST'])
    def links_page():
        if 'user_id' not in session:
            return redirect(url_for('login_page'))

        user_id = session.get('user_id')

        if request.method == 'POST':
            title = (request.form.get('title') or '').strip()
            slug = (request.form.get('slug') or '').strip()
            destination_url = (request.form.get('destination_url') or '').strip()

            try:
                new_link = AffiliateLink(
                    title=title,
                    slug=slug,
                    destination_url=destination_url,
                    user_id=user_id,
                )
                db.session.add(new_link)
                db.session.commit()
                flash("Affiliate link created!", "success")
                return redirect(url_for('links_page'))
            except Exception as e:
                db.session.rollback()
                print("❌ LINK INSERT ERROR:", e)
                flash(f"Could not save link: {e}", "danger")
                return redirect(url_for('links_page'))

        q = (request.args.get('q') or '').strip()
        click_filter = (request.args.get('click_filter') or 'all').strip()
        page = request.args.get('page', 1, type=int)
        per_page = 8

        try:
            query = AffiliateLink.query.filter_by(user_id=user_id)

            if q:
                like_pattern = f"%{q}%"
                query = query.filter(
                    db.or_(
                        AffiliateLink.title.ilike(like_pattern),
                        AffiliateLink.slug.ilike(like_pattern),
                        AffiliateLink.destination_url.ilike(like_pattern),
                    )
                )

            if click_filter == 'has_clicks':
                query = query.filter(AffiliateLink.click_count > 0)
            elif click_filter == 'zero_clicks':
                query = query.filter(AffiliateLink.click_count == 0)

            pagination = query.order_by(AffiliateLink.id.desc()).paginate(
                page=page, per_page=per_page, error_out=False
            )

            return render_template(
                'links.html',
                links=pagination.items,
                pagination=pagination,
                q=q,
                click_filter=click_filter,
            )
        except Exception as e:
            db.session.rollback()
            print("❌ LINK PAGE ERROR:", e)
            return str(e)

    @app.route('/view/scheduler', methods=['GET', 'POST'])
    def scheduler_page():
        if 'user_id' not in session:
            return redirect(url_for('login_page'))

        user_id = session.get('user_id', 1)
        if request.method == 'POST':
            title = request.form.get('title')
            platform = request.form.get('platform')
            scheduled_date = request.form.get('scheduled_date')
            scheduled_time = request.form.get('scheduled_time')
            ampm = request.form.get('ampm')
            content = request.form.get('content')
            print("TITLE =", title)
            print("PLATFORM =", platform)
            print("DATE =", scheduled_date)
            print("TIME =", scheduled_time)
            print("AMPM =", ampm)
            print("CONTENT =", content)
            print("USER_ID =", user_id)

            if title and platform and scheduled_date and scheduled_time and ampm and content:
                try:
                    time_str = f"{scheduled_time} {ampm}"
                    parsed_time = datetime.datetime.strptime(time_str, "%H:%M %p").time()

                    scheduled_for = datetime.datetime.combine(
                        datetime.datetime.fromisoformat(scheduled_date).date(),
                        parsed_time,
                    )

                    new_post = ScheduledPost(
                        title=title,
                        content=content,
                        platform=platform,
                        scheduled_for=scheduled_for,
                        status='scheduled',
                        user_id=user_id,
                    )
                    db.session.add(new_post)
                    db.session.commit()
                except Exception as e:
                    db.session.rollback()
                    print("SCHEDULER ERROR:", str(e))
                    return f"SCHEDULER ERROR: {str(e)}"
                print(" SCHEDULED POST SAVED")

        try:
            posts = ScheduledPost.query.all()
        except Exception as e:
            db.session.rollback()
            print("SCHEDULER PAGE ERROR:", str(e))
            return f"SCHEDULER PAGE ERROR: {str(e)}"
        return render_template('scheduler.html', posts=posts)

    @app.route('/view/ai-generator', methods=['GET', 'POST'])
    def ai_generator_page():
        if 'user_id' not in session:
            return redirect(url_for('login_page'))

        generated_result = None
        if request.method == 'POST':
            title = request.form.get('title')
            platform = request.form.get('platform')
            tone = request.form.get('tone', 'Engaging')

            if title and platform:
                try:
                    if not groq_client:
                        raise Exception("Groq client not initialized.")

                    prompt = (
                        f"Create a social media post for {platform}. "
                        f"Topic/Title: {title}. "
                        f"Tone: {tone}. "
                        f"Strictly format your response with two distinct sections using these exact labels: "
                        f"CAPTION: [Write the hook and professional caption here] "
                        f"HASHTAGS: [Write 8 to 10 space-separated trending hashtags starting with # here] "
                        f"Do not use emojis and do not use markdown bolding."
                    )

                    chat_completion = groq_client.chat.completions.create(
                        messages=[
                            {
                                "role": "system",
                                "content": "You are a professional social media manager. Provide clean plain text divided into CAPTION: and HASHTAGS: sections.",
                            },
                            {
                                "role": "user",
                                "content": prompt,
                            },
                        ],
                        model="openai/gpt-oss-120b",
                    )

                    ai_output = chat_completion.choices[0].message.content

                    caption_text = ai_output
                    hashtags_text = "#NewCar #FirstLook #CarVlog #CarReview #DriveWithMe #AutoEnthusiast #CarCommunity"

                    if "HASHTAGS:" in ai_output:
                        parts = ai_output.split("HASHTAGS:")
                        caption_text = parts[0].replace("CAPTION:", "").strip()
                        hashtags_text = parts[1].strip()
                    elif "Hashtags:" in ai_output:
                        parts = ai_output.split("Hashtags:")
                        caption_text = parts[0].replace("Caption:", "").strip()
                        hashtags_text = parts[1].strip()

                    generated_result = {
                        "title": title,
                        "platform": platform,
                        "caption": caption_text,
                        "hashtags": hashtags_text,
                    }
                except Exception as e:
                    generated_result = {
                        "title": title,
                        "platform": platform,
                        "caption": f"Failed to generate content via Groq API. Error: {str(e)}",
                        "hashtags": "",
                    }

        return render_template('ai_generator.html', result=generated_result)

    # Sponsorship & Revenue Tracker Routes
    @app.route('/view/finance', methods=['GET', 'POST'])
    def finance_page():
        if 'user_id' not in session:
            return redirect(url_for('login_page'))

        user_id = session.get('user_id')
        if request.method == 'POST':
            brand_name = request.form.get('brand_name')
            deal_title = request.form.get('deal_title')
            payout_amount = request.form.get('payout_amount')
            status = request.form.get('status', 'Pending')

            if brand_name and deal_title and payout_amount:
                try:
                    clean_amount = float(payout_amount)
                    new_deal = Sponsorship(
                        brand_name=brand_name,
                        deal_title=deal_title,
                        payout_amount=clean_amount,
                        status=status,
                        user_id=user_id,
                    )
                    db.session.add(new_deal)
                    db.session.commit()
                    flash("Sponsorship deal added.", "success")
                except Exception as e:
                    db.session.rollback()
                    print(f" [FINANCE ERROR] {e}")
                    flash("Failed to add sponsorship deal.", "danger")
            return redirect(url_for('finance_page'))

        try:
            deals = Sponsorship.query.filter_by(user_id=user_id).order_by(Sponsorship.id.desc()).all()
        except Exception as e:
            db.session.rollback()
            deals = []

        total_earnings = sum(d.payout_amount for d in deals if d.status == 'Completed')
        pending_earnings = sum(d.payout_amount for d in deals if d.status == 'Pending')

        return render_template(
            'finance.html',
            deals=deals,
            total_earnings=total_earnings,
            pending_earnings=pending_earnings,
        )

    @app.route('/view/finance/export.csv', methods=['GET'])
    def export_finance_csv():
        if 'user_id' not in session:
            return redirect(url_for('login_page'))

        user_id = session.get('user_id')
        try:
            deals = Sponsorship.query.filter_by(user_id=user_id).order_by(Sponsorship.id.desc()).all()
        except Exception as e:
            db.session.rollback()
            deals = []

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["ID", "Brand Name", "Deal Title", "Payout Amount", "Status"])

        for d in deals:
            writer.writerow([
                d.id,
                d.brand_name,
                d.deal_title,
                f"{float(d.payout_amount):.2f}" if d.payout_amount is not None else "0.00",
                d.status,
            ])

        csv_payload = output.getvalue()
        output.close()

        return Response(
            csv_payload,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=sponsorships_finance.csv"},
        )

    @app.route('/view/finance/update/<int:deal_id>', methods=['POST'])
    def update_finance_status(deal_id):
        if 'user_id' not in session:
            return redirect(url_for('login_page'))

        user_id = session.get('user_id')
        try:
            deal = db.session.get(Sponsorship, deal_id)
            if deal and deal.user_id == user_id:
                new_status = request.form.get('status')
                if new_status in ['Pending', 'Completed']:
                    deal.status = new_status
                    db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f" [FINANCE UPDATE ERROR] {e}")
        return redirect(url_for('finance_page'))

    @app.route('/profile', methods=['GET', 'POST'])
    def profile_page():
        if 'user_id' not in session:
            return redirect(url_for('login_page'))

        user_id = session.get('user_id')
        try:
            user = db.session.get(User, user_id)
        except Exception as e:
            db.session.rollback()
            user = None

        if not user:
            return redirect(url_for('login_page'))

        if request.method == 'POST':
            try:
                email_notifications = True if request.form.get('email_notifications') == 'true' else False
                user.email_notifications = email_notifications
                db.session.commit()
                flash("Profile settings saved.", "success")
            except Exception as e:
                db.session.rollback()
                print(f" [PROFILE ERROR] {e}")
                flash("Could not save profile settings.", "danger")
            return redirect(url_for('profile_page'))

        return render_template('profile.html', user=user)

    @app.route('/profile/avatar', methods=['POST'])
    def upload_avatar():
        if 'user_id' not in session:
            return redirect(url_for('login_page'))

        user_id = session.get('user_id')
        try:
            user = db.session.get(User, user_id)
            if not user:
                return redirect(url_for('login_page'))

            file = request.files.get('avatar')
            if not file or not file.filename:
                flash("Please choose an image file first.", "warning")
                return redirect(url_for('profile_page'))

            if not allowed_avatar_file(file.filename):
                flash("Unsupported format. Use PNG, JPG, JPEG, or WEBP.", "danger")
                return redirect(url_for('profile_page'))

            upload_dir = os.path.join(current_app.root_path, "static", "uploads", "avatars")
            os.makedirs(upload_dir, exist_ok=True)

            ext = file.filename.rsplit(".", 1)[1].lower()
            filename = f"user_{user.id}_{uuid.uuid4().hex[:10]}.{ext}"
            filepath = os.path.join(upload_dir, secure_filename(filename))
            file.save(filepath)

            user.avatar_url = url_for("static", filename=f"uploads/avatars/{filename}")
            db.session.commit()
            flash("Profile picture updated!", "success")
        except Exception as e:
            db.session.rollback()
            print(f" [AVATAR UPLOAD ERROR] {e}")
            flash("Failed to upload profile picture.", "danger")

        return redirect(url_for('profile_page'))

    # API endpoint to power Chart.js analytics time-series graph
    @app.route('/api/chart-analytics', methods=['GET'])
    def chart_analytics_api():
        if 'user_id' not in session:
            return jsonify({"labels": [], "data": []}), 401

        user_id = session.get('user_id')
        try:
            clicks = (
                db.session.query(ClickEvent)
                .join(AffiliateLink, ClickEvent.link_id == AffiliateLink.id)
                .filter(AffiliateLink.user_id == user_id)
                .order_by(ClickEvent.timestamp.asc())
                .all()
            )
        except Exception as e:
            db.session.rollback()
            print(f" [CHART API ERROR] {e}")
            clicks = []

        date_counts = {}
        for c in clicks:
            if c.timestamp:
                d_str = c.timestamp.strftime('%Y-%m-%d')
                date_counts[d_str] = date_counts.get(d_str, 0) + 1

        labels = list(date_counts.keys())
        data = list(date_counts.values())
        return jsonify({"labels": labels, "data": data}), 200

    @app.route('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login_page'))

    @app.route('/r/<slug>')
    def redirect_to_target(slug):
        try:
            link = AffiliateLink.query.filter_by(slug=slug).first_or_404()
            click = ClickEvent(
                link_id=link.id,
                ip_address=request.remote_addr,
                user_agent=request.headers.get('User-Agent'),
            )
            link.click_count = (link.click_count or 0) + 1
            db.session.add(click)
            db.session.commit()
            return redirect(link.destination_url, code=302)
        except Exception as e:
            db.session.rollback()
            return "Link not found or error occurred", 404

    @app.route('/links/delete/<int:link_id>', methods=['POST'])
    def delete_link(link_id):
        if 'user_id' not in session:
            return redirect(url_for('login_page'))

        user_id = session.get('user_id')
        try:
            link = db.session.get(AffiliateLink, link_id)
            if link and link.user_id == user_id:
                ClickEvent.query.filter_by(link_id=link.id).delete()
                db.session.delete(link)
                db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f" [DELETE LINK ERROR] {e}")
        return redirect(url_for('links_page'))

    @app.route('/notifications/clear', methods=['POST'])
    def clear_notifications():

        if 'user_id' not in session:
            return redirect(url_for('login_page'))

        user_id = session.get('user_id')

        try:
            Notification.query.filter_by(
                user_id=user_id
            ).delete()

            db.session.commit()

        except Exception as e:
            db.session.rollback()
            print("CLEAR NOTIFICATION ERROR:", e)

        return redirect(request.referrer or url_for('home'))

    @app.route('/view/finance/delete/<int:deal_id>', methods=['POST'])
    def delete_sponsorship(deal_id):
        if 'user_id' not in session:
            return redirect(url_for('login_page'))

        user_id = session.get('user_id')
        try:
            deal = db.session.get(Sponsorship, deal_id)
            if deal and deal.user_id == user_id:
                db.session.delete(deal)
                db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f" [DELETE SPONSORSHIP ERROR] {e}")
        return redirect(url_for('finance_page'))

    @app.route('/scheduler/delete/<int:post_id>', methods=['POST'])
    def delete_scheduled_post(post_id):

        if 'user_id' not in session:
            return redirect(url_for('login_page'))

        try:
            post = db.session.get(ScheduledPost, post_id)

            if post:
                db.session.delete(post)
                db.session.commit()

        except Exception as e:
            db.session.rollback()
            print("DELETE SCHEDULED POST ERROR:", e)


        return redirect(url_for('scheduler_page'))

    @app.route('/debug/db', methods=['GET'])
    def debug_database():
        try:
            inspector = db.inspect(db.engine)
            tables = inspector.get_table_names()

            user_count = User.query.count()
            link_count = AffiliateLink.query.count()
            all_links = [
                {"id": l.id, "title": l.title, "slug": l.slug, "user_id": l.user_id}
                for l in AffiliateLink.query.all()
            ]
            all_users = [
                {"id": u.id, "username": u.username, "email": u.email}
                for u in User.query.all()
            ]

            return jsonify({
                "status": "connected",
                "tables": tables,
                "user_count": user_count,
                "users": all_users,
                "link_count": link_count,
                "links": all_links,
            }), 200
        except Exception as e:
            db.session.rollback()
            return jsonify({
                "status": "error",
                "message": str(e),
            }), 500

    return app


app = create_app()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080))
    )