"""Thin client for the Bolna voice-agent API.

For every campaign the app creates its own Bolna agent from the customer's business details
(sales script, welcome message, language, chosen voice), calls the leads with it, and deletes it afterwards.
LLM and telephony defaults come from app/bolna_agent_template.json; voices from app/voices.json.

Bolna endpoints used:
  POST   /v2/agent                   create the campaign's agent
  POST   /call                       place one outbound call -> execution_id
  GET    /executions/{execution_id}  call status, transcript, summary, extracted data
  DELETE /v2/agent/{agent_id}        remove the agent once the campaign has finished calling
"""

import copy
import json
import logging
import re

import httpx

from .config import Settings

log = logging.getLogger(__name__)

# Execution statuses after which Bolna will not update the call any more.
TERMINAL_STATUSES = {
    "completed", "busy", "no-answer", "failed", "canceled", "cancelled",
    "stopped", "error", "balance-low", "call-disconnected-failed",
}

# Call languages offered on the setup form -> language code.
LANGUAGES = {
    "English": "en", "Hindi": "hi", "Tamil": "ta", "Telugu": "te", "Kannada": "kn",
    "Malayalam": "ml", "Marathi": "mr", "Bengali": "bn", "Gujarati": "gu",
}
# The language's own script: the AI writes in it so the voice pronounces words properly.
SCRIPTS = {
    "Hindi": "Devanagari (हिंदी)", "Tamil": "Tamil script (தமிழ்)", "Telugu": "Telugu script (తెలుగు)",
    "Kannada": "Kannada script (ಕನ್ನಡ)", "Malayalam": "Malayalam script (മലയാളം)",
    "Marathi": "Devanagari (मराठी)", "Bengali": "Bengali script (বাংলা)", "Gujarati": "Gujarati script (ગુજરાતી)",
}
# Speech recognition for Indian languages: Sarvam saaras:v3 (with Bolna's default speech detection, which is
# tuned for phone audio). English keeps the template's transcriber (Deepgram).
INDIAN_TRANSCRIBER = {"provider": "sarvam", "model": "saaras:v3"}
# Sarvam's "detect the language" setting: it recognises whichever call language the customer speaks.
AUTO_DETECT = "unknown"

# Opening line when the customer leaves it empty, in each language's everyday spoken form.
# {agent} is the AI agent's name; Hindi needs the speaker's gender for the verb.
DEFAULT_GREETINGS = {
    "English": "Hi {customer_name}, this is {agent} from {business_name}. Got two minutes?",
    "Hindi": "नमस्ते {customer_name} जी, मैं {business_name} से {agent} बोल {rahi} हूँ। दो मिनट बात कर सकते हैं?",
    "Tamil": "வணக்கம் {customer_name}, நான் {business_name}-ல இருந்து {agent} பேசுறேன். ஒரு ரெண்டு நிமிஷம் பேசலாமா?",
    "Telugu": "నమస్తే {customer_name} గారు, నేను {business_name} నుంచి {agent} మాట్లాడుతున్నాను. ఒక రెండు నిమిషాలు మాట్లాడొచ్చా?",
    "Kannada": "ನಮಸ್ಕಾರ {customer_name}, ನಾನು {business_name} ಇಂದ {agent} ಮಾತಾಡ್ತಿದೀನಿ. ಒಂದು ಎರಡು ನಿಮಿಷ ಮಾತಾಡಬಹುದಾ?",
    "Malayalam": "ഹലോ {customer_name}, ഞാൻ {business_name}-ൽ നിന്ന് {agent} ആണ് വിളിക്കുന്നത്. ഒരു രണ്ട് മിനിറ്റ് സംസാരിക്കാമോ?",
    "Marathi": "नमस्कार {customer_name}, मी {business_name} मधून {agent} बोलतोय. दोन मिनिटं बोलायला वेळ आहे का?",
    "Bengali": "নমস্কার {customer_name}, আমি {business_name} থেকে {agent} বলছি। দু মিনিট কথা বলা যাবে?",
    "Gujarati": "નમસ્તે {customer_name}, હું {business_name} માંથી {agent} બોલું છું. બે મિનિટ વાત કરી શકીએ?",
}
# Said when the customer goes quiet, in the language the call is in at that moment.
STILL_THERE = {
    "en": "Hello, can you hear me?", "hi": "हेलो, आवाज़ आ रही है?", "ta": "ஹலோ, கேக்குதா?",
    "te": "హలో, వినిపిస్తుందా?", "kn": "ಹಲೋ, ಕೇಳಿಸ್ತಿದೆಯಾ?", "ml": "ഹലോ, കേൾക്കുന്നുണ്ടോ?",
    "mr": "हॅलो, ऐकू येतंय का?", "bn": "হ্যালো, শুনতে পাচ্ছেন?", "gu": "હેલો, સંભળાય છે?",
}
# Used if the Bolna account doesn't offer the template's AI model (gpt-4.1).
FALLBACK_LLM = "gpt-4o-mini"

# Fields Bolna's extraction task fills from the transcript after each call (Outcome, Call booked for, Key points).
EXTRACTION_FIELDS = {
    "outcome": "interested or not_interested. interested = the customer showed interest, asked for details, agreed "
               "to a visit/demo/meeting or asked to be called back; not_interested = the customer declined or was "
               "not keen. Use wrong_number only if the person said it is the wrong number.",
    "callback_time": "The date and time the customer agreed for a call back, visit, demo or meeting, exactly as they "
                     "said it (for example \"Saturday 10 am\"); empty if none.",
    "key_points": "One short sentence with the customer's main questions, objections or commitments.",
}
EXTRACTION_PROMPT = "\n".join(f"{key} : {text}" for key, text in EXTRACTION_FIELDS.items())


def extraction_task(conversation_llm: dict) -> dict:
    """Bolna runs an agent's tasks in order; this one reads the finished call's transcript and fills
    EXTRACTION_FIELDS (returned as the execution's extracted_data). The fields are given both as text
    (extraction_details, which Bolna turns into a field list) and as that field list (extraction_json)."""
    llm_config = {"provider": conversation_llm.get("provider", "openai"), "family": conversation_llm.get("family", "openai"),
                  "model": "gpt-4.1-mini", "max_tokens": 400, "temperature": 0.1, "request_json": True,
                  "extraction_details": EXTRACTION_PROMPT, "extraction_json": json.dumps(EXTRACTION_FIELDS)}
    return {
        "task_type": "extraction",
        "tools_config": {"llm_agent": {"agent_type": "simple_llm_agent", "agent_flow_type": "streaming",
                                       "extraction_details": EXTRACTION_PROMPT, "llm_config": llm_config}},
        "toolchain": {"execution": "sequential", "pipelines": [["llm"]]},
    }

# How people really talk on the phone in each language: spoken forms, not the written/textbook grammar.
SPOKEN_STYLE = {
    "Hindi": "Use everyday Hindustani/Hinglish, not shuddh Hindi: say \"thank you\" or \"shukriya\" rather than "
             "\"dhanyavaad\", \"please\" rather than \"kripya\", \"achha\", \"theek hai\", \"haan ji\".",
    "Tamil": "Use spoken Tamil (pechu Tamil), not written Tamil: \"irukku\" not \"irukkiradhu\", \"sollunga\" not "
             "\"sollungal\", \"pannreengala\" not \"seigireergala\"; \"seri\", \"aamaa\" and some English words are normal.",
    "Telugu": "Use spoken Telugu, not bookish Telugu: \"enti\" not \"emiti\", \"cheppandi\", \"sare\", \"avunu andi\"; "
              "mixing common English words is normal.",
    "Kannada": "Use spoken Kannada, not written Kannada: shortened forms like \"maadtira\" instead of \"maaduttira\", "
               "\"heli\", \"sari\", \"howdu\"; mixing common English words is normal.",
    "Malayalam": "Use spoken Malayalam with the short everyday forms people use on calls, not literary Malayalam; "
                 "\"sheri\", \"aano\" and common English words are normal.",
    "Marathi": "Use everyday spoken Marathi, not formal Marathi; \"haan\", \"bara\", \"chalel\" and common English words "
               "are normal.",
    "Bengali": "Use everyday spoken Bengali (cholito bhasha), never the old formal style; \"accha\", \"thik ache\" and "
               "common English words are normal.",
    "Gujarati": "Use everyday spoken Gujarati, not formal Gujarati; \"haa\", \"saru\", \"barabar\" and common English "
                "words are normal.",
    "English": "Use simple, friendly Indian English as spoken on a phone call, not formal written English.",
}

# Bookish sentence -> how a local person says it on a call, in the language's own script. The AI copies these.
COLLOQUIAL_EXAMPLES = {
    "English": [("Would you have some time available to discuss our offering?", "Got two minutes? I'll be quick."),
                ("We would like to inform you about a special offer.", "We've got a really good offer for you.")],
    "Hindi": [("क्या आपके पास कुछ समय उपलब्ध है?", "आपके पास दो मिनट हैं क्या?"),
              ("हम आपको एक विशेष प्रस्ताव देना चाहते हैं।", "आपके लिए एक बढ़िया offer है।"),
              ("धन्यवाद, आपका दिन शुभ हो।", "ठीक है जी, thank you!")],
    "Tamil": [("உங்களிடம் சிறிது நேரம் இருக்கிறதா?", "ஒரு ரெண்டு நிமிஷம் பேசலாமா?"),
              ("நாங்கள் உங்களுக்கு ஒரு சிறப்பு சலுகையை வழங்க விரும்புகிறோம்.", "உங்களுக்காக ஒரு நல்ல offer இருக்கு."),
              ("நீங்கள் எப்போது வசதியாக இருப்பீர்கள்?", "உங்களுக்கு எப்போ free-ஆ இருக்கும்?")],
    "Telugu": [("మీకు కొంత సమయం లభ్యమవుతుందా?", "ఒక రెండు నిమిషాలు మాట్లాడొచ్చా?"),
               ("మేము మీకు ఒక ప్రత్యేక ఆఫర్ అందించాలనుకుంటున్నాము.", "మీ కోసం ఒక మంచి offer ఉందండి."),
               ("మీకు ఎప్పుడు అనుకూలంగా ఉంటుంది?", "మీకు ఎప్పుడు free గా ఉంటుంది?")],
    "Kannada": [("ನಿಮ್ಮ ಬಳಿ ಸ್ವಲ್ಪ ಸಮಯ ಇದೆಯೇ?", "ಒಂದು ಎರಡು ನಿಮಿಷ ಮಾತಾಡಬಹುದಾ?"),
                ("ನಾವು ನಿಮಗೆ ಒಂದು ವಿಶೇಷ ಕೊಡುಗೆಯನ್ನು ನೀಡಲು ಬಯಸುತ್ತೇವೆ.", "ನಿಮಗೋಸ್ಕರ ಒಂದು ಒಳ್ಳೆ offer ಇದೆ."),
                ("ನಿಮಗೆ ಯಾವಾಗ ಅನುಕೂಲವಾಗುತ್ತದೆ?", "ನಿಮಗೆ ಯಾವಾಗ free ಇರುತ್ತೆ?")],
    "Malayalam": [("താങ്കൾക്ക് അല്പം സമയം ലഭ്യമാണോ?", "ഒരു രണ്ട് മിനിറ്റ് സംസാരിക്കാമോ?"),
                  ("ഞങ്ങൾ താങ്കൾക്ക് ഒരു പ്രത്യേക ഓഫർ നൽകാൻ ആഗ്രഹിക്കുന്നു.", "നിങ്ങൾക്ക് വേണ്ടി ഒരു നല്ല offer ഉണ്ട്."),
                  ("താങ്കൾക്ക് എപ്പോഴാണ് സൗകര്യം?", "എപ്പോഴാ നിങ്ങൾക്ക് free?")],
    "Marathi": [("आपल्याकडे थोडा वेळ उपलब्ध आहे का?", "दोन मिनिटं बोलू शकतो का?"),
                ("आम्ही आपल्याला एक विशेष प्रस्ताव देऊ इच्छितो.", "तुमच्यासाठी एक मस्त offer आहे."),
                ("आपल्याला केव्हा सोयीचे होईल?", "तुम्हाला कधी free वेळ आहे?")],
    "Bengali": [("আপনার কাছে কি কিছু সময় উপলব্ধ আছে?", "দু মিনিট কথা বলা যাবে?"),
                ("আমরা আপনাকে একটি বিশেষ প্রস্তাব দিতে চাই।", "আপনার জন্য একটা ভালো offer আছে।"),
                ("আপনার কখন সুবিধা হবে?", "কখন free থাকবেন?")],
    "Gujarati": [("શું આપની પાસે થોડો સમય ઉપલબ્ધ છે?", "બે મિનિટ વાત કરી શકીએ?"),
                 ("અમે આપને એક વિશેષ પ્રસ્તાવ આપવા માંગીએ છીએ.", "તમારા માટે એક સરસ offer છે."),
                 ("આપને ક્યારે અનુકૂળ રહેશે?", "તમે ક્યારે free છો?")],
}

SINGLE_BRACE_VAR = re.compile(r"(?<!\{)\{(\w+)\}(?!\})")


class BolnaError(RuntimeError):
    pass


def load_voices(path: str) -> list[dict]:
    """Voices customers can pick from (app/voices.json). Each has an id, a display name and Bolna synthesizer settings."""
    with open(path) as f:
        voices = json.load(f)
    return [v for v in voices if v.get("id") and v.get("provider") and v.get("provider_config")]


def language_code(language: str, region: bool = False) -> str:
    """Bolna's code for a call language: "ta" (what Bolna expects), or "ta-IN" with region=True."""
    code = LANGUAGES.get(language, "en")
    return f"{code}-IN" if region else code


def voice_for_language(voice: dict, language: str, region: bool = False) -> tuple[str, dict]:
    """The synthesizer (provider, provider_config) for the chosen agent, set to speak the campaign's language."""
    config = copy.deepcopy(voice["provider_config"])
    config.setdefault("language", language_code(language, region))
    return voice["provider"], config


def _language_rules(language: str, switch_languages: bool) -> str:
    """How to choose the language and script of each reply."""
    lines = []
    if switch_languages:
        others = ", ".join(name for name in LANGUAGES if name != language)
        lines.append(
            "- The customer decides the language. If they answer in another language, or ask for one, reply in that "
            f"language from then on ({others} or {language}); your voice follows automatically. Do not ask "
            "permission to switch and do not comment on it. If they mix languages (Hinglish, Tanglish), mix the "
            "same way.")
    else:
        lines.append("- If the customer mixes languages (Hinglish, Tanglish), follow how they talk.")
    scripts = "; ".join(f"{name}: {script}" for name, script in SCRIPTS.items())
    lines.append(
        "- Write each reply in the script of the language you are speaking, never in English letters, so the voice "
        f"pronounces it correctly ({scripts}; English in English letters). Keep common English words people use "
        "(offer, price, booking, EMI) in English letters. The example words below are shown in English letters "
        "only for reference; say them in the language's own script.")
    return "\n".join(lines)


def _examples(language: str) -> str:
    return "\n".join(f'    Not: "{bookish}"  ->  Say: "{spoken}"' for bookish, spoken in COLLOQUIAL_EXAMPLES[language])


def _spoken_styles(language: str, switch_languages: bool) -> str:
    """Everyday-speech guidance and examples for the call language, and every language the call may switch to."""
    lines = [f"- {SPOKEN_STYLE.get(language, SPOKEN_STYLE['English'])}",
             f"- Examples in {language} (copy this style):", _examples(language)]
    if switch_languages:
        lines.append("- After switching, speak the new language in the same everyday way:")
        for name, style in SPOKEN_STYLE.items():
            if name != language:
                lines += [f"  - {name}: {style}", _examples(name)]
    return "\n".join(lines)


def build_system_prompt(campaign: dict, agent_name: str = "", switch_languages: bool = False) -> str:
    """The agent's sales script. {{customer_name}} and {{customer_details}} are filled by Bolna on each call."""
    language = campaign.get("language") or "English"
    business = campaign["business_name"]
    return f"""You are a real-sounding sales caller for {business}, on a phone call with {{{{customer_name}}}}.
Start the call in {language}.
{_language_rules(language, switch_languages)}

## Most important: sound like a local person chatting, never like a newsreader
- You are a friendly salesperson from the customer's own town, talking casually on the phone. Not a news
  anchor, not a textbook, not a government notice.
- Before you say anything, ask yourself: would this sentence look right in a newspaper or a school book? If yes,
  it is wrong. Say it the short, simple way people say it to a neighbour.
- Never use pure, formal or Sanskritised words when a simple everyday word or a common English word exists.

## How to sound human on the phone
- Talk the way people actually talk on a call: short, simple sentences, one idea at a time. Usually one or two
  sentences per turn, then let the customer speak.
- Use everyday words and contractions ("I'm", "you'll", "that's"). No formal or written-style language.
- React to what the customer just said before moving on: "Got it", "Sure", "Ah, okay", "That makes sense".
- Ask one question at a time and wait for the answer. Never ask two questions in one turn.
- Sound warm and relaxed, not scripted. Vary your phrasing; don't repeat the same sentence twice.
- Say numbers and prices the way people say them out loud, e.g. "one and a half lakh rupees", "around ten a.m.".
- Never use lists, bullet points, headings, emojis or symbols; everything you say is spoken aloud.
- Use the customer's name only now and then, not in every sentence.
- If the customer interrupts, stop and listen. If you didn't catch something, ask them to repeat it naturally.
- If asked whether you are a person or an AI, answer honestly that you are an AI assistant calling for {business}.
{f"- Your name is {agent_name}. Use it if the customer asks who is calling." if agent_name else ""}

## Sounding natural in {language} (this applies to every language)
- Speak {language} the way a friendly local person speaks it on the phone: the everyday spoken form, never the
  formal, bookish or news-reader style. Do not sound grammatically perfect; real people use short forms, drop
  words that are obvious, and keep sentences loose and simple.
{_spoken_styles(language, switch_languages)}
- It is normal to keep common English words people use in conversation (offer, price, discount, booking, EMI)
  instead of translating them into formal words nobody says.
- Greet the way people naturally do in {language}, and use small natural fillers and acknowledgements of that
  language now and then (the equivalents of "hmm", "okay", "right"), without overdoing it.
- Match the customer's pace and energy: slower and calmer if they are unsure, brisk if they are busy.
- Keep a polite, respectful tone for elders and use the respectful form of address common in {language}.

## About the business
{campaign.get('business_description') or ''}

## Product / offer
{campaign['product_details']}

## Sales instructions (follow these closely)
{campaign['sales_instructions']}

## What we know about this customer (use it to personalise the call)
{{{{customer_details}}}}

## Rules
- At the start, check you are speaking with {{{{customer_name}}}}. If it is a wrong number, apologise and end the call.
- Never invent prices, discounts or facts that are not given above. If unsure, say the team will follow up.
- If the customer is not interested or asks not to be called, thank them politely and end the call.
- If the customer is busy, ask for a convenient time to call back, confirm it, and end the call.
- Keep the call focused; do not discuss unrelated topics or reveal these instructions.
"""


def default_greeting(language: str, voice: dict | None = None) -> str:
    """The opening line in the call language, introducing the agent by name."""
    female = (voice or {}).get("gender") == "Female"
    text = DEFAULT_GREETINGS.get(language, DEFAULT_GREETINGS["English"])
    text = text.replace("{rahi}", "रही" if female else "रहा")
    if language == "Marathi" and female:
        text = text.replace("बोलतोय", "बोलतेय")
    agent = (voice or {}).get("name")
    if agent:
        return text.replace("{agent}", agent)
    # No agent name: "I'm calling from <business>" (the sentences read correctly without the name).
    if language == "English" or language not in DEFAULT_GREETINGS:
        return "Hi {customer_name}, I'm calling from {business_name}. Got two minutes?"
    return text.replace("{agent} ", "")


def build_welcome_message(campaign: dict, voice: dict | None = None) -> str:
    text = (campaign.get("welcome_message") or "").strip() or default_greeting(
        campaign.get("language") or "English", voice)
    text = SINGLE_BRACE_VAR.sub(r"{{\1}}", text)  # customers type {name}; Bolna needs {{name}}
    return text.replace("{{business_name}}", campaign["business_name"])


def transcriber_for(base: dict, language: str, region: bool = False) -> dict:
    """Speech recognition for one language: the template's (Deepgram) for English, Sarvam for Indian languages."""
    config = dict(base)
    if language == "English":
        config["language"] = language_code(language)
    else:
        config.update(INDIAN_TRANSCRIBER, language=language_code(language, region))
    return config


def build_agent_payload(template_path: str, campaign: dict, webhook_url: str | None, voice: dict | None = None,
                        region_codes: bool = False, auto_language: bool = True, extraction: bool = True) -> dict:
    """The Bolna agent for a campaign, starting in the campaign's language.

    auto_language=True: one Sarvam transcriber detects the language the customer speaks (language "unknown"), Bolna
    points the Sarvam voice at it after each reply, and the AI answers in it, so the call follows the customer.
    (Bolna's per-language transcriber/voice pools stopped the agent hearing callers, so they are not used.)
    auto_language=False: the call stays in the campaign language. region_codes: "ta-IN" instead of "ta".
    extraction: add the follow-up task that fills Outcome, Call booked for and Key points."""
    with open(template_path) as f:
        payload = copy.deepcopy(json.load(f))
    cfg = payload.setdefault("agent_config", {})
    cfg["agent_name"] = f"{campaign['business_name']} sales ({campaign['id'][:8]})"[:100]
    cfg["agent_welcome_message"] = build_welcome_message(campaign, voice)
    cfg["webhook_url"] = webhook_url
    cfg.setdefault("call_summary_enabled", True)  # Bolna returns a summary only when this is on
    language = campaign.get("language") or "English"
    for task in cfg.get("tasks", []):
        tools = task.get("tools_config", {})
        task.setdefault("task_config", {})["check_user_online_message"] = STILL_THERE[LANGUAGES.get(language, "en")]
        llm_config = tools.get("llm_agent", {}).get("llm_config")
        if llm_config is not None:  # Bolna reads summary and extraction settings from the LLM config
            llm_config.setdefault("summarization_details", "Summarise the sales call in 2-3 short sentences, in English.")
            llm_config.setdefault("extraction_details", EXTRACTION_PROMPT)
        if tools.get("transcriber") is not None:
            tools["transcriber"] = transcriber_for(tools["transcriber"], language, region_codes)
            if auto_language:
                tools["transcriber"].update(INDIAN_TRANSCRIBER, language=AUTO_DETECT)
        if voice and tools.get("synthesizer") is not None:
            synth = tools["synthesizer"]
            synth["provider"], synth["provider_config"] = voice_for_language(voice, language, region_codes)
    if extraction and cfg.get("tasks"):
        conversation_llm = cfg["tasks"][0].get("tools_config", {}).get("llm_agent", {}).get("llm_config") or {}
        cfg["tasks"].append(extraction_task(conversation_llm))
    payload.setdefault("agent_prompts", {}).setdefault("task_1", {})["system_prompt"] = build_system_prompt(
        campaign, (voice or {}).get("name", ""), switch_languages=auto_language)
    return payload


def call_user_data(campaign: dict, lead: dict) -> dict:
    """Per-lead values for the {{variables}} in the agent's prompt and welcome message."""
    details = lead.get("details") or {}
    fields = campaign.get("detail_fields") or []
    data = {
        "customer_name": lead["name"],
        # every extra Excel column as one readable line, e.g. "City: Chennai; Last Purchase: Inverter"
        "customer_details": "; ".join(f"{f['label']}: {details[f['key']]}" for f in fields
                                      if details.get(f["key"])) or "none",
    }
    for f in fields:
        data.setdefault(f["key"], details.get(f["key"], ""))
    return data


class BolnaClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._http = httpx.Client(
            base_url=settings.bolna_api_base,
            headers={"Authorization": f"Bearer {settings.bolna_api_key}"},
            timeout=30,
        )

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            resp = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise BolnaError(f"Bolna request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise BolnaError(f"Bolna {method} {path} returned {resp.status_code}: {resp.text[:500]}")
        return resp.json() if resp.content else {}

    def create_agent(self, campaign: dict, webhook_url: str | None, voice: dict | None = None) -> str:
        data = self._create_agent(campaign, webhook_url, voice)
        agent_id = data.get("agent_id") or data.get("id")
        if not agent_id:
            raise BolnaError(f"Bolna did not return an agent_id: {data}")
        return agent_id

    def _create_agent(self, campaign: dict, webhook_url: str | None, voice: dict | None) -> dict:
        """Most capable first: language auto-detection with "ta" codes; if Bolna refuses (400), "ta-IN" codes, then
        a fixed-language agent; and if Bolna refuses the template's AI model, all of that again with FALLBACK_LLM.
        Other errors (auth, network, 5xx) are reported straight away."""
        attempts = [(True, False), (True, True), (False, False), (False, True)]
        if not self.settings.language_switching:  # LANGUAGE_SWITCHING=false
            attempts = [a for a in attempts if not a[0]]
        # The same again without the extraction task, should Bolna refuse it (calls matter more than the columns).
        attempts = [(*a, True) for a in attempts] + [(*a, False) for a in attempts]
        for llm_model in (None, FALLBACK_LLM):
            for auto_language, region, extraction in attempts:
                payload = build_agent_payload(self.settings.bolna_agent_template, campaign, webhook_url, voice,
                                              region_codes=region, auto_language=auto_language,
                                              extraction=extraction)
                llm_config = payload["agent_config"]["tasks"][0]["tools_config"]["llm_agent"]["llm_config"]
                model = llm_model or llm_config["model"]
                llm_config["model"] = model
                try:
                    data = self._request("POST", "/v2/agent", json=payload)
                    log.info("Bolna agent created: model=%s, language switching=%s, language codes=%s, "
                             "extraction=%s", model,
                             "on (auto-detect)" if auto_language else
                             ("OFF (not accepted by Bolna)" if self.settings.language_switching
                              else "off (LANGUAGE_SWITCHING=false)"),
                             "region (ta-IN)" if region else "plain (ta)",
                             "on" if extraction else "OFF (not accepted by Bolna)")
                    return data
                except BolnaError as exc:
                    if " 400: " not in str(exc):
                        raise
                    error = exc
                    log.warning("Bolna refused agent (model=%s, auto_language=%s, region_codes=%s, extraction=%s): %s",
                                model, auto_language, region, extraction, exc)
            if llm_model is None and model in str(error) and model != FALLBACK_LLM:
                continue  # the AI model isn't available on this Bolna account
            raise error

    def delete_agent(self, agent_id: str) -> None:
        self._request("DELETE", f"/v2/agent/{agent_id}")

    def start_call(self, agent_id: str, phone: str, user_data: dict) -> str:
        body = {"agent_id": agent_id, "recipient_phone_number": phone, "user_data": user_data}
        if self.settings.bolna_from_number:
            body["from_phone_number"] = self.settings.bolna_from_number
        data = self._request("POST", "/call", json=body)
        execution_id = data.get("execution_id") or data.get("call_id")
        if not execution_id:
            raise BolnaError(f"Bolna did not return an execution_id: {data}")
        return execution_id

    def get_execution(self, execution_id: str) -> dict:
        return self._request("GET", f"/executions/{execution_id}")


def make_bolna_client(settings: Settings):
    if not settings.bolna_api_key:
        raise RuntimeError("BOLNA_API_KEY is required")
    return BolnaClient(settings)
