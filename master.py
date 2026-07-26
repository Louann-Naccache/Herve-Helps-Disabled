import sys
import base64
from io import BytesIO
from PIL import Image
from openai import OpenAI

def safe_screenshot():
    """
    Tente de prendre une capture d'écran.
    Si pyautogui plante, on renvoie None au lieu de faire crasher le script.
    """
    try:
        import pyautogui
        img = pyautogui.screenshot()
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
        return f"data:image/png;base64,{b64}"
    except Exception as e:
        print("⚠️ Impossible de prendre une capture d'écran :", e)
        return None

def main():
    if len(sys.argv) < 2:
        print('Usage: python master.py "votre texte"')
        sys.exit(1)

    user_prompt = sys.argv[1]

    prompt2 = (
        f"Je veux : {user_prompt}. "
        "Mais qu'est-ce que je peux faire sur mon ordinateur pour faire cela ? "
        "Je suis une personne âgée, aide-moi pas à pas avec des repères visuels."
    )

    # La clé API doit être dans une variable d'environnement : OPENAI_API_KEY
    client = OpenAI(api_key="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")

    image_url = safe_screenshot()

    # Construction du message
    input_content = [{"type": "input_text", "text": prompt2}]
    if image_url:
        input_content.append({"type": "input_image", "image_url": image_url})

    # Appel du modèle
    response = client.responses.create(
        model="gpt-4o-mini",
        input=[{"role": "user", "content": input_content}],
    )

    print(response.output_text)

if __name__ == "__main__":
    main()
