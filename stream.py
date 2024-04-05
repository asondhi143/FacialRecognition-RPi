from flask import Flask, render_template, Response, request, redirect, url_for
import picamera
import io
import cv2
import face_recognition
import os
import numpy as np
from gpiozero import MotionSensor, Button
from time import sleep
import RPi.GPIO as GPIO
from threading import Thread, Lock
import sys
from statistics import mean
from twilio.rest import Client
import time

account_sid = os.getenv('TWILIO_ACCOUNT_SID')
auth_token = os.getenv('TWILIO_AUTH_TOKEN')
twilio_phone_number = os.getenv('TWILIO_PHONE_NUMBER')
your_phone_number = os.getenv('YOUR_PHONE_NUMBER')
client = Client(account_sid, auth_token)

app = Flask(__name__)
flask_app_active = False

known_faces_dir = "known_faces"
known_faces = {}

for file_name in os.listdir(known_faces_dir):
    if file_name.endswith(".jpg"):
        person_name = os.path.splitext(file_name)[0]
        file_path = os.path.join(known_faces_dir, file_name)
        known_image = face_recognition.load_image_file(file_path)
        known_faces[person_name] = face_recognition.face_encodings(known_image)[0]

recognized_person_camera = "Unknown"

GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
red_pin = 21
green_pin = 26
solenoid_pin = 18
GPIO.setup(red_pin, GPIO.OUT)
GPIO.setup(green_pin, GPIO.OUT)
GPIO.setup(solenoid_pin, GPIO.OUT)

lcd_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.append(lcd_dir)

try:
    from lcd.drivers import Lcd
except ImportError:
    print("Error: 'Lcd' class not found in the 'drivers' module. Make sure the 'lcd' directory is in the correct location.")
    sys.exit(1)

display = Lcd()
GPIO.output(red_pin, 1)
GPIO.output(green_pin, 0)

def set_color(red, green, message):
    GPIO.output(red_pin, red)
    GPIO.output(green_pin, green)

    display.lcd_clear()  # Clear the LCD screen

    if len(message) <= 16:
        display.lcd_display_string(message.ljust(16), 1)
    else:
        lines = [message[i:i + 16] for i in range(0, len(message), 16)]
        for i, line in enumerate(lines):
            display.lcd_display_string(line.ljust(16), i + 1)

camera_lock = Lock()

def solenoid_lock(lock):
    GPIO.output(solenoid_pin, not lock)
    print("Door is ", "Locked" if lock else "Unlocked")

def save_known_face(image_file, person_name):
    # Save the image file to the known_faces directory
    image_path = os.path.join(known_faces_dir, f"{person_name}.jpg")
    image_file.save(image_path)

    # Update the known_faces dictionary
    known_image = face_recognition.load_image_file(image_path)
    known_faces[person_name] = face_recognition.face_encodings(known_image)[0]

def remove_known_face(person_name):
    # Remove the image file from the known_faces directory
    image_path = os.path.join(known_faces_dir, f"{person_name}.jpg")
    if os.path.exists(image_path):
        os.remove(image_path)

    # Remove the person from the known_faces dictionary
    if person_name in known_faces:
        del known_faces[person_name]

pir = MotionSensor(4)
button = Button(17)

camera = None
use_website = False  # Set to True when Flask app is active

def initialize_camera():
    global camera
    for _ in range(5):  # Retry up to 5 times
        try:
            camera = picamera.PiCamera()
            camera.resolution = (640, 480) 
            return True
        except picamera.exc.PiCameraMMALError as e:
            print(f"Error: {e}")
            print("Retrying camera initialization...")
            sleep(1)
    return False

if not initialize_camera():
    print("Failed to initialize the camera. Exiting.")
    sys.exit(1)

facial_recognition_active = True  # Flag to control facial recognition process

def facial_recognition_process():
    global recognized_person_camera, flask_app_active, use_website, facial_recognition_active

    #camera.resolution = (640, 480) 
    camera.framerate = 15

    stream = io.BytesIO()
    capture_frames = False
    recognition_in_progress = False  # Flag to track if facial recognition is in progress
    last_motion_time = 0 
    while True:
        with camera_lock:
            stream = io.BytesIO()

            for _ in camera.capture_continuous(stream, 'jpeg', use_video_port=True):
                stream.seek(0)

                try:
                    frame = cv2.imdecode(np.frombuffer(stream.read(), dtype=np.uint8), 1)
                except cv2.error as e:
                    print(f"Error decoding frame: {e}")
                    continue

                small_frame = cv2.resize(frame, (0, 0), fx=0.5, fy=0.5)

                if facial_recognition_active:  # Check if facial recognition should run
                    face_locations = face_recognition.face_locations(small_frame)
                    face_encodings = face_recognition.face_encodings(small_frame, face_locations)

                    if pir.motion_detected:
                        current_time = time.time()
                        if current_time - last_motion_time > 10:  # Set a suitable cooldown period (e.g., 10 seconds)
                            last_motion_time = current_time
                        if not recognition_in_progress:
                            print("Facial Recognition is in progress")
                            recognition_in_progress = True
                            default_message = "Hello, Please look into camera"
                            set_color(1, 0, default_message) 

                        capture_frames = True
                        for (top, right, bottom, left), face_encoding in zip(face_locations, face_encodings):
                            matches = face_recognition.compare_faces(list(known_faces.values()), face_encoding, tolerance=0.5)
                            face_distance = face_recognition.face_distance(list(known_faces.values()), face_encoding)

                            if True in matches and min(face_distance) < 0.5:
                                matched_index = matches.index(True)
                                recognized_person_camera = list(known_faces.keys())[matched_index]
                                greeting_message = f"Hello {recognized_person_camera}"
                                set_color(0, 1, greeting_message)  # Display "Hello Name" on the first line
                                solenoid_lock(False)  # Unlock solenoid
                                sleep(5)  # Wait for 5 seconds
                                set_color(0, 1, "Welcome to the building!")
                                sleep(5)
                                message = client.messages.create(
                                    body=f"Attention! The person named {recognized_person_camera} has entered the building.",
                                    from_=twilio_phone_number,
                                    to=your_phone_number
                                )
                            else:
                                recognized_person_camera = "Unknown"
                                set_color(1, 0, "Door Locked. Access Denied")
                                solenoid_lock(True)  # Lock solenoid
                    else:
                        if recognition_in_progress:
                            print("Facial Recognition completed")
                            recognition_in_progress = False

                        if capture_frames:
                            print("No motion. Stopping facial recognition.")
                            capture_frames = False
                            set_color(1, 0, "Door Locked. Access Denied")
                            solenoid_lock(True)  # Lock solenoid

                stream.seek(0)
                stream.truncate()

                if not pir.motion_detected and not capture_frames:
                    break

        sleep(1)

pir_thread = Thread(target=pir.wait_for_motion)
pir_thread.start()

lcd_thread = Thread(target=display.lcd_clear)
lcd_thread.start()

led_thread = Thread(target=GPIO.output, args=(red_pin, 1))
led_thread.start()

unlock_timer = 0
unlock_thread = None

def unlock_door():
    global unlock_timer, unlock_thread
    set_color(0, 1, "Door is Unlocked")
    solenoid_lock(False)
    unlock_timer = 15
    sleep(5)
    set_color(0, 1, "Please Enter the building!")
    sleep(5)
    GPIO.output(green_pin, 1)

    if unlock_thread and unlock_thread.is_alive():
        unlock_thread.join()

    unlock_thread = Thread(target=unlock_timer_thread)
    unlock_thread.start()

def unlock_timer_thread():
    global unlock_timer
    while unlock_timer > 0:
        sleep(1)
        unlock_timer -= 1
        if unlock_timer == 0:
            set_color(1, 0, "Door Locked")
            GPIO.output(green_pin, 0)
            solenoid_lock(True)  # Lock solenoid

@app.route('/unlock_door', methods=['POST'])
def unlock_door_route():
    unlock_door()
    return "", 204  # No content response


def handle_button_press():
    message = client.messages.create(
        body="Somebody is waiting outside to enter the building.",
        from_=twilio_phone_number,
        to=your_phone_number
    )
    print("Button pressed! Message sent.")
    set_color(1, 0, "Owner has been informed")

# Add a new thread to listen for button press
def button_listener():
    while True:
        button.wait_for_press()
        handle_button_press()

# Start the button listener thread
button_thread = Thread(target=button_listener)
button_thread.start()



def start_flask_app():
    global flask_app_active, use_website
    flask_app_active = True
    use_website = True
    app.run(host='0.0.0.0', port=5000, debug=False)

def start_camera():
    while True:
        stream = io.BytesIO()

        with camera_lock:
            for _ in camera.capture_continuous(stream, 'jpeg', use_video_port=True):
                stream.seek(0)
                frame = stream.read()
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')

                stream.seek(0)
                stream.truncate()

                sleep(0.2)

@app.route('/test_motion')
def test_motion():
    pir.wait_for_motion()
    return "Motion detected!"

@app.route('/video_feed')
def video_feed():
    return Response(start_camera(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/add_remove_users', methods=['GET', 'POST'])
def add_remove_users():
    global flask_app_active, use_website, facial_recognition_active

    if not flask_app_active or not use_website:
        return redirect(url_for('index'))

    person_names = list(known_faces.keys())

    if request.method == 'POST':
        if 'add_known_face' in request.form:
            person_name = request.form['person_name']
            image_file = request.files['image_file']

            if person_name and image_file:
                facial_recognition_active = False  # Pause facial recognition
                save_known_face(image_file, person_name)
                facial_recognition_active = True  # Resume facial recognition
                return redirect(url_for('index'))

        elif 'remove_known_face' in request.form:
            person_name = request.form['person_name']

            if person_name:
                facial_recognition_active = False  # Pause facial recognition
                remove_known_face(person_name)
                facial_recognition_active = True  # Resume facial recognition
                return redirect(url_for('index'))

    return render_template('add_remove_users.html', person_names=person_names)

try:
    # Start Flask app in a separate thread
    flask_app_thread = Thread(target=start_flask_app)
    flask_app_thread.start()

    # Keep the main thread (facial recognition) running
    facial_recognition_thread = Thread(target=facial_recognition_process)
    facial_recognition_thread.start()

    flask_app_thread.join()
    facial_recognition_thread.join()

except KeyboardInterrupt:
    print("Keyboard interrupt detected. Cleaning up...")
    
    # Stop camera and release resources
    if camera:
        camera.close()

    # Clean up GPIO
    GPIO.cleanup()
    

    # Close the PIR sensor
    if pir:
        pir.close()

    print("Cleanup complete. Exiting.")
    sys.exit(0)
