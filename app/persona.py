"""Questionnaire + persona prompt builder. This is what makes the twin 'think like you'."""

QUESTIONS = [
    # (id, section, question, hint)
    ("full_name", "Basics", "What's your name, and what do people usually call you?", "e.g. Alex — friends call me Al"),
    ("age_place", "Basics", "How old are you, where do you live, and where did you grow up?", ""),
    ("languages", "Basics", "Which languages do you speak, and do you mix them when you talk?", "e.g. English + Telugu, I switch mid-sentence with family"),
    ("family", "Life", "Tell me about your family and the people closest to you.", "Names help — your twin will recognise them"),
    ("work", "Life", "What do you do for work, and what do you actually spend your days on?", ""),
    ("career_story", "Life", "What's the short story of how you got where you are?", ""),
    ("proud", "Life", "What are you most proud of?", ""),
    ("goals", "Life", "What are you working toward right now (next 1–3 years)?", ""),
    ("day", "Life", "Describe a typical day, from waking up to sleeping.", ""),
    ("likes_food", "Likes & dislikes", "Favourite foods, drinks, restaurants? Anything you refuse to eat?", ""),
    ("likes_media", "Likes & dislikes", "Favourite movies, shows, music, books, podcasts?", ""),
    ("hobbies", "Likes & dislikes", "What do you do for fun? Sports, hobbies, side projects?", ""),
    ("loves", "Likes & dislikes", "Things you love that you could talk about for hours?", ""),
    ("dislikes", "Likes & dislikes", "Things that annoy you or that you can't stand?", ""),
    ("pet_peeves", "Likes & dislikes", "Your biggest pet peeves with people?", ""),
    ("values", "Mind", "What values matter most to you? What won't you compromise on?", ""),
    ("beliefs", "Mind", "Strong opinions you hold (about work, tech, life, anything)?", ""),
    ("decisions", "Mind", "How do you make big decisions? Gut or spreadsheet? Who do you ask?", ""),
    ("stress", "Mind", "How do you react when stressed, angry, or under pressure?", ""),
    ("advice", "Mind", "What advice do you find yourself giving people again and again?", ""),
    ("unsure", "Mind", "What do you do when you don't know the answer to something?", ""),
    ("personality", "Personality", "Describe your personality in 5 words. How would your best friend describe you?", ""),
    ("introvert", "Personality", "Introvert or extrovert? How are you with strangers vs friends?", ""),
    ("humor", "Personality", "What's your sense of humour like? Give an example of a joke you'd make.", "sarcastic, dad jokes, dry, silly…"),
    ("talk_style", "How you talk", "How do you talk? Fast/slow, formal/casual, long/short answers?", "This matters a lot for sounding like you"),
    ("phrases", "How you talk", "Phrases, slang, or filler words you use a lot?", "e.g. 'basically', 'da', 'makes sense?', 'honestly'"),
    ("greeting", "How you talk", "How do you usually greet people and say goodbye?", ""),
    ("texting", "How you talk", "How do you text? Emojis, punctuation, lowercase, voice notes?", ""),
    ("disagree", "How you talk", "How do you disagree with someone or say no?", ""),
    ("topics_avoid", "Boundaries", "Topics your twin should avoid or never discuss?", "e.g. salary, politics, health, ex-partners"),
    ("private", "Boundaries", "Anything private your twin should never reveal, even if asked?", ""),
    ("stories", "Stories", "Tell 2–3 stories you love telling (funny, formative, embarrassing).", "The more detail, the more 'you' it feels"),
    ("memories", "Stories", "Key memories: childhood, school, first job, big moves?", ""),
    ("anything_else", "Stories", "Anything else your twin should know to be you?", ""),
]

REQUIRED = {"full_name", "talk_style"}


def build_system_prompt(twin: dict, answers: dict, memories: list[dict], voice_mode: bool) -> str:
    name = twin.get("display_name") or "the user"
    lines = [
        f"You ARE {name}. You are {name}'s digital twin and stand in for {name} as their replacement: "
        f"people talking to you should feel they are talking to {name}.",
        f"Always speak in the first person as {name} ('I', 'my'), with {name}'s opinions, memories, humour, "
        f"vocabulary and way of talking described below. Never describe {name} in the third person, "
        "never say you are an assistant or a language model, and never break character for small talk.",
        "",
        "Rules you must keep:",
        f"- Honesty about being an AI: if someone sincerely asks whether they are talking to the real {name}, "
        f"a human, or an AI, say plainly that you are {name}'s AI twin, then carry on in character.",
        f"- Don't invent big life facts (jobs, relationships, events) that aren't in your profile. If you don't know, "
        f"answer the way {name} would when unsure, or say you'll get back to them.",
        f"- Don't make commitments on {name}'s behalf (money, contracts, legal, medical, meetings, promises). "
        "Say you'll check and get back to them.",
        "- Respect the boundaries listed in your profile; deflect those topics the way you naturally would.",
        "- Match your own speaking style: answer length, slang, filler words, humour.",
    ]
    if voice_mode:
        lines.append("- You are speaking out loud: reply in 1-2 short natural spoken sentences (under 30 words). No markdown, lists, "
                     "emojis or stage directions.")
    else:
        lines.append("- You are texting: write like your own texting style.")

    lines += ["", f"=== WHO I AM ({name}) ==="]
    qmap = {q[0]: q for q in QUESTIONS}
    for qid, ans in answers.items():
        if qid in qmap and ans and str(ans).strip():
            lines.append(f"Q: {qmap[qid][2]}\nA: {str(ans).strip()}")

    if twin.get("speech_transcript"):
        lines += ["", "=== HOW I ACTUALLY TALK (a sample of me speaking; copy the rhythm and vocabulary, "
                      "NEVER repeat its sentences or treat what it describes as current news) ===",
                  twin["speech_transcript"][:3000]]
    if twin.get("writing_samples"):
        lines += ["", "=== THINGS I HAVE WRITTEN (copy the style, not the content) ===", twin["writing_samples"][:4000]]

    facts = [m for m in memories if m["kind"] == "fact"]
    fixes = [m for m in memories if m["kind"] == "correction"]
    if facts:
        lines += ["", "=== MORE FACTS ABOUT ME ==="] + [f"- {m['content']}" for m in facts[-60:]]
    if fixes:
        lines += ["", "=== HOW I REALLY ANSWER (I corrected my twin on these; follow them closely) ==="]
        for m in fixes[-30:]:
            lines.append(f"When someone said: \"{m['prompt']}\"\nI'd answer: \"{m['content']}\"")
    return "\n".join(lines)
