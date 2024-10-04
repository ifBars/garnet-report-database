from flask import Flask, render_template, request, redirect, url_for, flash, send_file, abort, jsonify
from flask_bootstrap import Bootstrap # type: ignore
from urllib.parse import urlparse
from datetime import datetime, timedelta
from bans import Ban, BanScraper, BanDatabase
import pandas as pd
import sqlite3
import os
import json

app = Flask(__name__)
app.config.from_object('config.Config')
Bootstrap(app)
CONFIG_FILE = 'config.json'

def read_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as file:
            return json.load(file)
    else:
        return {}

def write_config(config):
    with open(CONFIG_FILE, 'w') as file:
        json.dump(config, file, indent=4)

config = read_config()
UPLOAD_FOLDER = config.get('UPLOAD_FOLDER', 'E:\\Garnet-Reports')
DATABASE = 'reports.db'
BANDATABASEFILE = 'bans.db'
BAN_DATABASE = BanDatabase()
BANS_URL = "https://garnetgaming.net/darkrp/bans"

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def get_ban_db():
    conn = sqlite3.connect(BANDATABASEFILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with app.app_context():
        db = get_db()
        cursor = db.cursor()
        cursor.execute("""
            SELECT name FROM sqlite_master WHERE type='table' AND name='report';
        """)
        if not cursor.fetchone():
            with app.open_resource('schema.sql', mode='r') as f:
                db.executescript(f.read())
            db.commit()
        db.close()

def import_data_from_csv(file_path):
    with app.app_context():
        if os.path.exists(file_path):
            try:
                data = pd.read_csv(file_path, header=None, on_bad_lines='skip')
                data.columns = ['Date/Time', 'Reporter', 'Reportee', 'Report Reason', 'Evidence', 'Punishment']
                
                db = get_db()
                cursor = db.cursor()
                for _, row in data.iterrows():
                    try:
                        date_time = datetime.strptime(row['Date/Time'], '%m/%d/%Y %I:%M %p')
                        cursor.execute('INSERT INTO report (date_time, reporter, reportee, report_reason, evidence, punishment) VALUES (?, ?, ?, ?, ?, ?)',
                                       (date_time, row['Reporter'], row['Reportee'], row['Report Reason'], row['Evidence'], row['Punishment']))
                    except ValueError as e:
                        print(f"Error parsing date '{row['Date/Time']}': {e}")
                db.commit()
                db.close()
            except pd.errors.ParserError as e:
                print(f"Error reading CSV file: {e}")

if not os.path.exists(DATABASE):
    init_db()

@app.route('/')
def index():
    db = get_db()
    cursor = db.cursor()
    
    current_month = datetime.now().strftime('%Y-%m')
    first_day_of_current_month = datetime.now().replace(day=1)
    previous_month = (first_day_of_current_month - timedelta(days=1)).strftime('%Y-%m')
    selected_month = request.args.get('selected_month', previous_month if request.args.get('deep_storage') == 'true' else current_month)
    search_query = request.args.get('search_query', '').strip()
    search_field = request.args.get('search_field', 'all')
    sort_by = request.args.get('sort_by', 'date_time')
    sort_order = request.args.get('sort_order', 'DESC')
    deep_storage = request.args.get('deep_storage', 'false')
    query = "SELECT * FROM report WHERE 1=1"
    params = []
    
    if deep_storage == 'false':
        query += " AND strftime('%Y-%m', date_time) = ?"
        params.append(current_month)
    else:
        query += " AND strftime('%Y-%m', date_time) = ?"
        params.append(selected_month)

    if search_query:
        if search_field == 'all':
            query += " AND (reporter LIKE ? OR reportee LIKE ? OR report_reason LIKE ? OR punishment LIKE ?)"
            like_query = f'%{search_query}%'
            params.extend([like_query, like_query, like_query, like_query])

            try:
                date_query = datetime.strptime(search_query, '%Y-%m-%d')
                query += " OR date(date_time) = ?"
                params.append(date_query.strftime('%Y-%m-%d'))
            except ValueError:
                try:
                    month_query = datetime.strptime(search_query, '%Y-%m')
                    query += " OR strftime('%Y-%m', date_time) = ?"
                    params.append(month_query.strftime('%Y-%m'))
                except ValueError:
                    pass

        elif search_field in ['reporter', 'reportee', 'punishment']:
            query += f" AND {search_field} LIKE ?"
            like_query = f'%{search_query}%'
            params.append(like_query)

        elif search_field == 'date':
            try:
                date_query = datetime.strptime(search_query, '%Y-%m-%d')
                query += " AND date(date_time) = ?"
                params.append(date_query.strftime('%Y-%m-%d'))
            except ValueError:
                flash('Invalid date format. Use YYYY-MM-DD.', 'danger')
                return redirect(url_for('index'))

        elif search_field == 'month':
            try:
                month_query = datetime.strptime(search_query, '%Y-%m')
                query += " AND strftime('%Y-%m', date_time) = ?"
                params.append(month_query.strftime('%Y-%m'))
            except ValueError:
                flash('Invalid month format. Use YYYY-MM.', 'danger')
                return redirect(url_for('index'))

    if sort_by == 'month':
        query += " ORDER BY strftime('%Y-%m', date_time) " + sort_order
    else:
        query += f" ORDER BY {sort_by} {sort_order}"

    reports = cursor.execute(query, params).fetchall()
    report_count = len(reports)

    reports_list = []
    for report in reports:
        report_dict = dict(report)
        try:
            report_dict['date_time'] = datetime.strptime(report_dict['date_time'], '%Y-%m-%d %H:%M:%S')
        except ValueError as e:
            print(f"Error parsing date '{report_dict['date_time']}': {e}")

        if "ban" in report_dict['punishment'].lower() or "propban" in report_dict['punishment'].lower():
            try:
                parts = report_dict['punishment'].split()
                if len(parts) < 2:
                    report_dict['ban_status'] = "Invalid Duration"
                    continue

                duration_str = parts[0]
                duration_value = int(duration_str)
                unit = parts[1].lower()

                if "day" in unit:
                    ban_end_date = report_dict['date_time'] + timedelta(days=duration_value)
                elif "week" in unit:
                    ban_end_date = report_dict['date_time'] + timedelta(weeks=duration_value)
                elif "month" in unit:
                    ban_end_date = report_dict['date_time'] + timedelta(days=duration_value * 30)
                elif "hour" in unit or "hr" in unit:
                    ban_end_date = report_dict['date_time'] + timedelta(hours=duration_value)
                else:
                    ban_end_date = None

                if ban_end_date:
                    report_dict['ban_status'] = "Active" if datetime.now() < ban_end_date else "Expired"
                else:
                    report_dict['ban_status'] = "Unknown"
            except (ValueError, IndexError):
                report_dict['ban_status'] = "Invalid Duration"
        else:
            report_dict['ban_status'] = "N/A"
            
        evidence_list = report_dict['evidence'].split(',') if report_dict['evidence'] else []
        formatted_evidence = []
        for evidence in evidence_list:
            evidence = evidence.strip()
            if evidence.startswith('http://') or evidence.startswith('https://'):
                formatted_evidence.append({'type': 'link', 'url': evidence})
            else:
                formatted_evidence.append({'type': 'file', 'path': evidence})

        report_dict['evidence'] = json.dumps(formatted_evidence)
        reports_list.append(report_dict)

    db.close()

    return render_template('index.html', reports=reports_list, search_query=search_query, search_field=search_field, sort_by=sort_by, sort_order=sort_order, deep_storage=deep_storage, selected_month=selected_month, report_count=report_count)

@app.route('/bans', methods=['GET'])
def bans():
    db = get_ban_db()
    cursor = db.cursor()

    search_query = request.args.get('search_query', '').strip()
    search_field = request.args.get('search_field', 'all')
    sort_by = request.args.get('sort_by', 'date')
    sort_order = request.args.get('sort_order', 'DESC')

    query = "SELECT * FROM bans WHERE 1=1"
    params = []

    # Implementing search functionality
    if search_query:
        if search_field == 'all':
            query += " AND (player_name LIKE ? OR admin_name LIKE ? OR reason LIKE ?)"
            like_query = f'%{search_query}%'
            params.extend([like_query, like_query, like_query])
        elif search_field == 'player_name':
            query += " AND player_name LIKE ?"
            params.append(f'%{search_query}%')
        elif search_field == 'admin_name':
            query += " AND admin_name LIKE ?"
            params.append(f'%{search_query}%')
        elif search_field == 'reason':
            query += " AND reason LIKE ?"
            params.append(f'%{search_query}%')
        elif search_field == 'date':
            try:
                date_query = datetime.strptime(search_query, '%Y-%m-%d')
                query += " AND date(date) = ?"
                params.append(date_query.strftime('%Y-%m-%d'))
            except ValueError:
                flash('Invalid date format. Use YYYY-MM-DD.', 'danger')
                return redirect(url_for('bans'))

    # Implementing sorting functionality
    if sort_by in ['date', 'player_name', 'admin_name', 'reason']:
        query += f" ORDER BY {sort_by} {sort_order}"

    bans = cursor.execute(query, params).fetchall()

    # Converting results to a list of dictionaries
    bans_list = []
    for ban in bans:
        ban_dict = dict(ban)
        bans_list.append(ban_dict)

    db.close()

    return render_template('bans.html', bans=bans_list, search_query=search_query, search_field=search_field, sort_by=sort_by, sort_order=sort_order)

@app.route('/scrape_bans', methods=['POST'])
def scrape_bans():
    scraper = BanScraper(BANS_URL, request.form['steam_id'].strip())
    bans = scraper.scrape_bans()
    BAN_DATABASE.insert_bans(bans)
    return redirect(url_for('bans'))

@app.route('/add', methods=['GET', 'POST'])
def add_report():
    if request.method == 'POST':
        try:
            date_time = datetime.strptime(request.form['date_time'], '%Y-%m-%dT%H:%M')
        except ValueError:
            flash('Invalid date/time format.', 'danger')
            return redirect(url_for('add_report'))

        reporter = request.form['reporter']
        reportee = request.form['reportee']

        report_reasons = request.form.getlist('report_reason')
        if 'Other' in report_reasons:
            other_reason = request.form.get('other_reason', '').strip()
            if other_reason:
                report_reasons = ['Other']
                report_reasons.append(other_reason)

        report_reason = ', '.join(report_reasons)

        evidence = request.form.get('evidence', '').strip()  # Use empty string if not provided
        punishment = request.form['punishment']

        db = get_db()
        cursor = db.cursor()
        cursor.execute('INSERT INTO report (date_time, reporter, reportee, report_reason, evidence, punishment) VALUES (?, ?, ?, ?, ?, ?)',
                       (date_time, reporter, reportee, report_reason, evidence, punishment))
        db.commit()
        db.close()

        flash('Report added successfully!', 'success')
        if request.form['submit_type'] == 'add_report':
            return redirect(url_for('index'))
        elif request.form['submit_type'] == 'add_report_and_create_another':
            return redirect(url_for('add_report'))

    return render_template('add_report.html')

@app.route('/add_ban', methods=['GET', 'POST'])
def add_ban():
    if request.method == 'POST':
        try:
            date_time = datetime.strptime(request.form['date_time'], '%Y-%m-%dT%H:%M')
        except ValueError:
            flash('Invalid date/time format.', 'danger')
            return redirect(url_for('add_ban'))

        banned = request.form['banned_user']

        ban_reasons = request.form.getlist('ban_reason')
        if 'Other' in ban_reasons:
            other_reason = request.form.get('other_reason', '').strip()
            if other_reason:
                ban_reasons = ['Other']
                ban_reasons.append(other_reason)

        ban_reason = ', '.join(ban_reasons)

        evidence = request.form.get('evidence', '').strip()  # Use empty string if not provided
        length = request.form['length']
        ban_item = Ban(date_time, banned, "", "You", "", evidence, length, ban_reason)
        BAN_DATABASE.insert_ban(ban_item)

        flash('Ban added successfully!', 'success')
        if request.form['submit_type'] == 'add_ban':
            return redirect(url_for('bans'))
        elif request.form['submit_type'] == 'add_ban_and_create_another':
            return redirect(url_for('add_ban'))

    return render_template('add_ban.html')

@app.route('/edit/<int:id>', methods=['GET', 'POST'])
def edit_report(id):
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        try:
            date_time = datetime.strptime(request.form['date_time'], '%Y-%m-%dT%H:%M')
        except ValueError:
            flash('Invalid date/time format.', 'danger')
            return redirect(url_for('edit_report', id=id))

        reporter = request.form['reporter']
        reportee = request.form['reportee']

        report_reasons = request.form.getlist('report_reason')
        if 'Other' in report_reasons:
            other_reason = request.form.get('other_reason', '').strip()
            if other_reason:
                report_reasons = ['Other']
                report_reasons.append(other_reason)

        report_reason = ', '.join(report_reasons)

        evidence = request.form.get('evidence', '').strip()  # Use empty string if not provided
        punishment = request.form['punishment']

        cursor.execute('UPDATE report SET date_time = ?, reporter = ?, reportee = ?, report_reason = ?, evidence = ?, punishment = ? WHERE id = ?',
                       (date_time, reporter, reportee, report_reason, evidence, punishment, id))
        db.commit()
        db.close()
        flash('Report updated successfully!', 'success')
        return redirect(url_for('index'))

    report = cursor.execute('SELECT * FROM report WHERE id = ?', (id,)).fetchone()
    if report:
        report = dict(report)
        report['date_time'] = datetime.strptime(report['date_time'], '%Y-%m-%d %H:%M:%S')

        reasons_list = report['report_reason'].split(', ')
        if 'Other' in reasons_list:
            report['other_reason'] = reasons_list.pop()
        else:
            report['other_reason'] = ''

        report['report_reason'] = ', '.join(reasons_list)

    db.close()
    return render_template('edit_report.html', report=report)

@app.route('/delete/<int:id>')
def delete_report(id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('DELETE FROM report WHERE id = ?', (id,))
    db.commit()
    db.close()
    flash('Report deleted successfully!', 'success')
    return redirect(url_for('index'))

@app.route('/stats')
def stats():
    db = get_db()
    cursor = db.cursor()

    total_reports = cursor.execute('SELECT COUNT(*) FROM report').fetchone()[0]
    reports_per_reporter = cursor.execute('SELECT reporter, COUNT(*) FROM report GROUP BY reporter').fetchall()
    reports_per_reportee = cursor.execute('SELECT reportee, COUNT(*) FROM report GROUP BY reportee').fetchall()
    reports_per_reason = cursor.execute('SELECT report_reason, COUNT(*) FROM report GROUP BY report_reason').fetchall()
    monthly_reports = cursor.execute('SELECT strftime("%Y-%m", date_time) as month, COUNT(*) FROM report GROUP BY month').fetchall()
    db.close()

    reports_per_reporter = [{'label': row['reporter'], 'value': row[1]} for row in reports_per_reporter]
    reports_per_reportee = [{'label': row['reportee'], 'value': row[1]} for row in reports_per_reportee]
    reports_per_reason = [{'label': row['report_reason'], 'value': row[1]} for row in reports_per_reason]
    monthly_reports = [{'label': row['month'], 'value': row[1]} for row in monthly_reports]
    return render_template('stats.html', total_reports=total_reports,
                           reports_per_reporter=reports_per_reporter,
                           reports_per_reportee=reports_per_reportee,
                           reports_per_reason=reports_per_reason,
                           monthly_reports=monthly_reports)

@app.route('/stream_file/<path:file_path>')
def stream_file(file_path):
    if not file_path or '..' in file_path:
        flash('Invalid file path.', 'danger')
        return redirect(url_for('index'))
    safe_path = os.path.join(UPLOAD_FOLDER, file_path)
    #print(f"Safe path: {safe_path}")  # Debug print
    if os.path.isfile(safe_path):
        return send_file(safe_path, as_attachment=True)
    else:
        flash('File not found.', 'danger')
        return redirect(url_for('index'))

@app.route('/save_hotkey', methods=['POST'])
def save_hotkey():
    hotkey = request.form['shortcut'].strip()
    
    if hotkey:
        try:
            with open('config.json', 'r+') as config_file:
                config = json.load(config_file)
                config['shortcut'] = hotkey
                write_config(config)
                flash('Hotkey saved successfully!', 'success')
        except Exception as e:
            flash('Error saving hotkey!', 'danger')
    else:
        flash('Invalid hotkey!', 'danger')
        
    return redirect(url_for('settings'))

@app.route('/settings', methods=['GET'])
def settings():
    return render_template('settings.html', upload_folder=UPLOAD_FOLDER)

@app.route('/update_settings', methods=['POST'])
def update_settings():
    global UPLOAD_FOLDER

    if 'upload_folder' in request.form:
        new_upload_folder = request.form['upload_folder'].strip()
        if os.path.isdir(new_upload_folder):
            UPLOAD_FOLDER = new_upload_folder
            config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
            write_config(config)
            flash('Settings updated successfully!', 'success')
        else:
            flash('Invalid folder path. Please ensure the folder exists.', 'danger')

    if 'import_csv' in request.form:
        csv_path = request.form['csv_path'].strip()
        if os.path.exists(csv_path):
            import_data_from_csv(csv_path)
            flash('CSV data imported successfully!', 'success')
        else:
            flash('Invalid CSV file path. Please ensure the file exists.', 'danger')

    return redirect(url_for('settings'))

@app.route('/autocomplete', methods=['GET'])
def autocomplete():
    query = request.args.get('query', '')
    if not query:
        return jsonify([])
    
    conn = sqlite3.connect('reports.db')
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT reporter FROM report WHERE reporter LIKE ? UNION SELECT DISTINCT reportee FROM report WHERE reportee LIKE ?", ('%' + query + '%', '%' + query + '%'))
    names = cursor.fetchall()
    conn.close()
    names = [name[0] for name in names]
    return jsonify(names)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=4200)
