from datetime import datetime
import io
import os
import os.path
import uuid
import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file
import psycopg2
from psycopg2.extras import RealDictCursor
import qrcode
import traceback

app = Flask(__name__)

# Configuración de la Base de Datos PostgreSQL
def get_db_connection():
    # Render busca automáticamente la variable 'DATABASE_URL' en la nube
    database_url = os.environ.get("DATABASE_URL")

    if database_url:
        # Conexión automática cuando está publicado en Render
        conn = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    else:
        # Conexión local para cuando lo pruebes en tu propia computadora
        conn = psycopg2.connect(
            host="localhost",
            database="db_asistencias_jjsz",
            user="db_asistencias_jjsz_user",
            password="",
            cursor_factory=RealDictCursor,
        )
    return conn
def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Crear tabla de eventos si no existe
    cur.execute('''
        CREATE TABLE IF NOT EXISTS eventos (
            id SERIAL PRIMARY KEY,
            titulo VARCHAR(150) NOT NULL,
            descripcion TEXT,
            fecha_evento TIMESTAMP NOT NULL,
            periodo VARCHAR(20) NOT NULL,
            token_qr VARCHAR(100) UNIQUE NOT NULL,
            creado_en TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    
    # Crear tabla de asistencias si no existe
    cur.execute('''
        CREATE TABLE IF NOT EXISTS asistencias (
            id SERIAL PRIMARY KEY,
            evento_id INTEGER REFERENCES eventos(id) ON DELETE CASCADE,
            nombre_alumno VARCHAR(150) NOT NULL,
            correo_alumno VARCHAR(150) NOT NULL,
            carrera VARCHAR(100) NOT NULL,
            fecha_registro TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            ip_dispositivo VARCHAR(50),
            CONSTRAINT unique_asistencia_evento UNIQUE (evento_id, correo_alumno)
        );
    ''')
    
    conn.commit()
    cur.close()
    conn.close()

# Ejecutar la creación al iniciar la app
init_db()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/escanear")
def escanear():
    return render_template("escanear.html")


# Ruta para crear el evento y generar su código QR único con IP automática
@app.route("/crear-evento", methods=["POST"])
def crear_evento():
    datos = request.get_json(silent=True) or request.form
    titulo = datos.get("titulo")
    descripcion = datos.get("descripcion")
    fecha_evento = datos.get("fecha_evento")
    periodo = datos.get("periodo")

    if not titulo or not fecha_evento or not periodo:
        return (
            jsonify({"error": "Faltan datos obligatorios (título, fecha o periodo)"}),
            400,
        )

    token_qr = str(uuid.uuid4())

    conn = get_db_connection()
    cur = conn.cursor()

    try:
        cur.execute(
            """
                INSERT INTO eventos (titulo, descripcion, fecha_evento, periodo, token_qr)
                VALUES (%s, %s, %s, %s, %s) RETURNING id;
            """,
            (titulo, descripcion, fecha_evento, periodo, token_qr),
        )

        evento_id = cur.fetchone()["id"]
        conn.commit()
        cur.close()
        conn.close()

        os.makedirs("static/qrs", exist_ok=True)

        # Detecta automáticamente la IP y el puerto actual para que el QR nunca falle
        host_actual = request.host
        enlace_web = f"http://{host_actual}/escanear?token={token_qr}"
        
        qr_img = qrcode.make(enlace_web)
        qr_path = os.path.join("static", "qrs", f"evento_{evento_id}.png")
        qr_img.save(qr_path)

        return (
            jsonify({
                "mensaje": "¡Evento creado y código QR generado con éxito!",
                "token_qr": token_qr,
                "evento_id": evento_id,
                "qr_imagen": f"/static/qrs/evento_{evento_id}.png",
            }),
            201,
        )

    except Exception as e:
        conn.rollback()
        cur.close()
        conn.close()
        return jsonify({"error": str(e)}), 500


# Ruta para registrar la asistencia del alumno al escanear el QR con candado por IP
@app.route("/registrar-asistencia", methods=["POST"])
def registrar_asistencia():
    datos = request.json
    token_qr = datos.get("token_qr")
    nombre_alumno = datos.get("nombre")
    correo_alumno = datos.get("correo")
    carrera = datos.get("carrera")

    if not token_qr or not nombre_alumno or not correo_alumno or not carrera:
        return jsonify({"error": "Faltan datos obligatorios en el formulario"}), 400

    ip_dispositivo = request.remote_addr

    conn = get_db_connection()
    cur = conn.cursor()

    try:
        cur.execute("SELECT id FROM eventos WHERE token_qr = %s;", (token_qr,))
        evento = cur.fetchone()

        if not evento:
            cur.close()
            conn.close()
            return jsonify({"error": "El código QR del evento no es válido."}), 404

        evento_id = evento["id"]

        cur.execute(
            """
                SELECT 1 FROM asistencias 
                WHERE evento_id = %s AND ip_dispositivo = %s;
            """,
            (evento_id, ip_dispositivo),
        )

        if cur.fetchone():
            cur.close()
            conn.close()
            return (
                jsonify({
                    "error": (
                        "Este dispositivo ya registró una asistencia para este"
                        " evento."
                    )
                }),
                400,
            )

        cur.execute(
            """
                INSERT INTO asistencias (evento_id, nombre_alumno, correo_alumno, carrera, ip_dispositivo)
                VALUES (%s, %s, %s, %s, %s);
            """,
            (evento_id, nombre_alumno, correo_alumno, carrera, ip_dispositivo),
        )

        conn.commit()
        cur.close()
        conn.close()

        return jsonify({"mensaje": "¡Asistencia registrada con éxito!"}), 201

    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        cur.close()
        conn.close()
        return (
            jsonify({
                "error": (
                    "Este correo ya registró su asistencia para este evento."
                )
            }),
            400,
        )
    except Exception as e:
        conn.rollback()
        cur.close()
        conn.close()
        return jsonify({"error": str(e)}), 500


# Ruta para exportar la lista de asistencias a Excel con formato profesional
@app.route("/exportar-asistencias/<int:evento_id>", methods=["GET"])
def exportar_asistencias(evento_id):
    conn = get_db_connection()
    cur = conn.cursor()

    try:
        cur.execute("SELECT titulo FROM eventos WHERE id = %s;", (evento_id,))
        evento = cur.fetchone()

        if not evento:
            cur.close()
            conn.close()
            return jsonify({"error": "Evento no encontrado"}), 404

        cur.execute(
            """
                SELECT nombre_alumno, correo_alumno, carrera, fecha_registro 
                FROM asistencias 
                WHERE evento_id = %s 
                ORDER BY fecha_registro ASC;
            """,
            (evento_id,),
        )
        asistencias = cur.fetchall()
        cur.close()
        conn.close()

        if not asistencias:
            return jsonify({"error": "No hay registros de asistencia para este evento."}), 404

        df = pd.DataFrame(asistencias)
        df.columns = ['Nombre del Alumno', 'Correo Institucional', 'Carrera', 'Hora de Registro']

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Asistencias')
            worksheet = writer.sheets['Asistencias']
            for col in worksheet.columns:
                max_length = max(len(str(cell.value or '')) for cell in col)
                col_letter = col[0].column_letter
                worksheet.column_dimensions[col_letter].width = max(max_length + 4, 15)

        output.seek(0)
        nombre_archivo = f"Asistencias_{evento['titulo'].replace(' ', '_')}.xlsx"

        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=nombre_archivo
        )

    except Exception as e:
        cur.close()
        conn.close()
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True)
