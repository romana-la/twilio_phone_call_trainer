from dotenv import load_dotenv
from flask import Flask, request, session
from twilio.twiml.voice_response import VoiceResponse, Gather
from openai import OpenAI
# load configuration from .env
load_dotenv()

# initialize Flask application
app = Flask(__name__)
app.config.from_prefixed_env()

INPUT_QUESTIONS = {
    "language": "Hello, what language do you want to practice today? For German, press 1. For French, press 2. For Italian, press 3.",
    "level": "What is your level? For beginner, press 1. For immediate, press 2. For advanced, press 3.",
    "scenario": "What scenario do you want to practice? For making an appointment at the doctor's, press 1. For ordering food at a take-away restaurant, press 2. For a job interview, press 3. ",
}

ALL_INPUT_CHOICES = {
    "language": {
        "1": "German",
        "2": "French",
        "3": "Italian",
    },
    "level": {
        "1": "beginner",
        "2": "intermediate",
        "3": "advanced",
    },
    "scenario": {
        "1": "doctor",
        "2": "take-away",
        "3": "interview",
    },
}

# Read out loud a question to the user, collect their input (DTMF tone) and send it as an HTTP request to /handle_input/{input_type}
def _gather_digit_input(input_type):
    gather = Gather(num_digits=1, action=f"/handle_input/{input_type}")
    gather.say(INPUT_QUESTIONS[input_type])
    return gather


@app.route("/answer_call", methods=["GET", "POST"])
def answer_call():
    # Start the TwiML response
    resp = VoiceResponse()


    # Invoke the _gather_digit_input function, which will read out loud a question and collect the user's input
    resp.append(_gather_digit_input("language"))


    # If the user doesn't enter any digits on their keyboard for 5 seconds, start again
    resp.redirect("/answer_call")


    return str(resp)

@app.route("/handle_input/<input_type>", methods=["GET", "POST"])
# Processes the user's input of a specific type (language/level/scenario)
def handle_input(input_type):


    # Check if the request contains the Digits parameter
    if "Digits" not in request.values:
        abort(HTTPStatus.BAD_REQUEST, "Parameter not found: Digits")
        return


    # Check if the input type is valid
    try:
        possible_choices = ALL_INPUT_CHOICES[input_type]
    except KeyError:
        abort(HTTPStatus.BAD_REQUEST, f"Invalid input type: {input_type}")
        return


    # Start the TwiML response
    resp = VoiceResponse()


    # Get the digit the caller chose from the Digits parameter
    choice = request.values["Digits"]


    # Check if the digit the caller chose is within the supported range
    if choice not in possible_choices:
        resp.say("Sorry, I don't understand that choice.")
        resp.redirect("/answer_call")
        return str(resp)


    # Add the caller's choice into the session
    session[input_type] = possible_choices[choice]


    # Determine which input type comes next
    next_input_type = {
        "language": "level",
        "level": "scenario",
        "scenario": None,
    }[input_type]


    if next_input_type is not None:
        # If not all the input is collected yet, invoke the _gather_digit_input function again to collect the next input type
        resp.append(_gather_digit_input(next_input_type))
    else:
        # Once all the input is collected, create new conversation history containing the initial prompt for GPT
        conversation_log = _start_conversation_log()

        # Pass on the conversation history to GPT to get its response
        message = _ask_gpt(conversation_log)

        # Read out the GPT's response to the user, collect their input (speech) and send it as an HTTP request to /handle_chat
        resp.append(_gather_chat_response(message))

    # If the user doesn't provide any input, start again
    resp.redirect("/answer_call")
    return str(resp)

def _start_conversation_log():
    # Formulate a prompt based on the user’s choices
    language, level = session["language"], session["level"]
    system_prompts = {
        "interview": f"You are a recruiter making a phone interview with a job candidate. Start the conversation by introducing yourself (make up a name) and the position (make it up). Don't speak for the applicant. Speak {language} at {level} level. Do not use any other language than {language}. Don't introduce your line. The whole conversation should consist of around 5 turns for each participant. Finish your last turn by saying goodbye",
        "doctor": f"You are a receptionist at a doctor’s office. You just picked up a phone call from a patient. Start the conversation by introducing yourself and the doctor (make up the names) and ask how you can help them. Don't speak for the patient. Speak {language} at {level} level. Do not use any other language than {language}. Don't introduce your line. The whole conversation should consist of around 5 turns for each participant. Finish your last turn by saying goodby",
        "take-away": f"You are a waiter at a restaurant offering take-away food. You just picked up a phone call from a customer. Start the conversation by introducing yourself and the restaurant (make up the names) and ask how you can help them. Don't speak for the customer. Speak {language} at {level} level. Do not use any other language than {language}. Don't introduce your line. The whole conversation should consist of around 5 turns for each participant. Finish your last turn by saying goodbye",
    }
    prompt = system_prompts[session["scenario"]]

    # Create conversation log consisting of the prompt
    return [{"role": "system", "content": prompt}]


def _ask_gpt(conversation_log):
    client = OpenAI()
    # Make an API call to OpenAI with the conversation history in the messages parameter
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=conversation_log,
        temperature=1,
        max_tokens=256,
        top_p=1,
        frequency_penalty=0,
        presence_penalty=0,
    )
    message = response.choices[0].message.content

    # Append GPT's response into the conversation history
    conversation_log.append({"role": "assistant", "content": message})

    # Update the session with the modified conversation log
    session["conversation_log"] = conversation_log

    return message

def _gather_chat_response(message):
    # Map the language name to an IETF BCP 47 language tag Twilio understands
    language_to_language_code = {
        "German": "de-DE",
        "French": "fr-FR",
        "Italian": "it-IT",
    }
    language_code = language_to_language_code[session["language"]]

    gather = Gather(
        input="speech",
        action=f"/handle_chat",
        language=language_code,
    )
    gather.say(message, language=language_code)

    return gather

@app.route("/handle_chat", methods=["GET", "POST"])
def handle_chat():
    # Check if the request contains user's response
    if "SpeechResult" not in request.values:
        abort(HTTPStatus.BAD_REQUEST, "Parameter not found: SpeechResult")
        return

    # Start the TwiML response
    resp = VoiceResponse()

    # Extract the conversation history from the session
    conversation_log = session.get("conversation_log")
    if not conversation_log:
        resp.say("Something went wrong.")
        resp.redirect("/answer_call")
        return str(resp)

    # Extract the user's response from the request and append it to the conversation history
    conversation_log.append({"role": "user", "content": request.values["SpeechResult"]})

    # Make an API call to GPT with the updated conversation history in the messages parameter
    message = _ask_gpt(conversation_log)
    resp.append(_gather_chat_response(message))

    return str(resp)

if __name__ == "__main__":
    app.run(debug=True)

