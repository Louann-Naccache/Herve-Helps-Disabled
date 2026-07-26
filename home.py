#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=========================================================================
 LOGICIEL D'ASSISTANCE VOCALE ACCESSIBLE
=========================================================================
Un logiciel graphique (bleu & blanc, très décoratif) avec :
  - Un bouton MICRO : enregistre la voix puis la transcrit via l'API
    OpenAI (Whisper). Le texte obtenu est stocké dans la variable
    globale `paroles_dites_TXT`.
  - Une zone de texte (au-dessus du bouton micro) où afficher
    n'importe quel texte (utilisez app.afficher_texte("...")).
  - Un panneau d'ACCESSIBILITÉ complet. Certaines options agissent sur
    TOUT L'ORDINATEUR (pas seulement le logiciel), tant que celui-ci
    reste ouvert :
        * Contraste élevé système (Windows)
        * Loupe Windows (zoom système) + zoom +/-
        * Clavier visuel (OSK)
        * Narrateur Windows on/off
        * Volume système +/-/muet
        * Raccourcis clavier GLOBAUX (fonctionnent même si l'app n'a
          pas le focus) : Ctrl+Alt+M (micro), Ctrl+Alt+L (lire le
          texte à voix haute), Ctrl+Alt+C (contraste), Ctrl+Alt+Haut/
          Bas (volume)
        * Lecture à voix haute (synthèse vocale) du texte affiché
        * Agrandissement/réduction de la taille du texte affiché

-------------------------------------------------------------------------
INSTALLATION DES DEPENDANCES :

    pip install openai sounddevice numpy pyttsx3 keyboard

Remarques :
  - Le module "keyboard" nécessite parfois d'exécuter le script en
    administrateur (Windows) ou avec sudo (Linux) pour capter les
    raccourcis clavier au niveau du système.
  - Les actions "contraste", "loupe", "clavier visuel", "narrateur"
    utilisent des fonctions propres à Windows. Sur macOS/Linux, ces
    boutons resteront visibles mais afficheront un message
    d'iExplorerndisponibilité plutôt que de planter.
  - Remplacez OPENAI_API_KEY ci-dessous par votre propre clé API.
=========================================================================
"""
import ctypes
import sys
import os
import wave
import random
import ctypes
import tempfile
import platform
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, font as tkfont, messagebox



# ---- Dépendances optionnelles : le logiciel démarre même si l'une
# ---- d'entre elles est absente, en désactivant juste la fonction liée.
try:
    import sounddevice as sd
    import numpy as np
    AUDIO_DISPONIBLE = True
except ImportError:
    AUDIO_DISPONIBLE = False

try:
    from openai import OpenAI
    OPENAI_DISPONIBLE = True
except ImportError:
    OPENAI_DISPONIBLE = False

try:
    import pyttsx3
    TTS_DISPONIBLE = True
except ImportError:
    TTS_DISPONIBLE = False

try:
    import keyboard
    KEYBOARD_DISPONIBLE = True
except ImportError:
    KEYBOARD_DISPONIBLE = False

IS_WINDOWS = platform.system() == "Windows"

# =========================================================================
# CONFIGURATION
# =========================================================================
OPENAI_API_KEY = "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"  # <-- A REMPLACER

SAMPLE_RATE = 16000
CANAUX = 1

# Palette bleu & blanc
BLEU_NUIT       = "#08306B"
BLEU_FONCE      = "#0B4F9E"
BLEU_PRINCIPAL  = "#1E6FD9"
BLEU_CLAIR      = "#8FC1FF"
BLEU_TRES_CLAIR = "#EAF2FF"
BLANC           = "#FFFFFF"
GRIS_TEXTE      = "#0A2A55"

# Variable globale demandée : contient le dernier texte transcrit par le micro
paroles_dites_TXT = ""


# =========================================================================
# ACCESSIBILITE SYSTEME (agit sur tout l'ordinateur, pas que le logiciel)
# =========================================================================
class HIGHCONTRAST(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("dwFlags", ctypes.c_uint),
        ("lpszDefaultScheme", ctypes.c_wchar_p),
    ]


SPI_SETHIGHCONTRAST = 0x0043
HCF_HIGHCONTRASTON = 0x00000001
SPIF_UPDATEINIFILE = 0x01
SPIF_SENDCHANGE = 0x02

VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
KEYEVENTF_KEYUP = 0x0002


class AccessibiliteSysteme:
    """Regroupe toutes les actions qui influent sur TOUT l'ordinateur,
    tant que le logiciel est ouvert. Sur Windows, on utilise l'API
    Win32 via ctypes. Sur les autres systèmes, on informe simplement
    l'utilisateur que l'action n'est pas disponible."""

    def __init__(self, on_message=None):
        self.on_message = on_message or (lambda msg: None)
        self.contraste_actif = False
        self.hotkeys_actifs = False
        self.moteur_tts = pyttsx3.init() if TTS_DISPONIBLE else None
        self._hotkeys_ids = []

    def _signaler(self, message):
        self.on_message(message)

    # --- Contraste élevé système ---
    def basculer_contraste_eleve(self):
        if not IS_WINDOWS:
            self._signaler("Contraste système : disponible uniquement sous Windows.")
            return
        try:
            hc = HIGHCONTRAST()
            hc.cbSize = ctypes.sizeof(HIGHCONTRAST)
            hc.lpszDefaultScheme = None
            hc.dwFlags = HCF_HIGHCONTRASTON if not self.contraste_actif else 0
            ctypes.windll.user32.SystemParametersInfoW(
                SPI_SETHIGHCONTRAST, ctypes.sizeof(HIGHCONTRAST),
                ctypes.byref(hc), SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
            )
            self.contraste_actif = not self.contraste_actif
            etat = "activé" if self.contraste_actif else "désactivé"
            self._signaler(f"Contraste élevé système {etat}.")
        except Exception as e:
            self._signaler(f"Impossible de changer le contraste système : {e}")

    # --- Loupe Windows (zoom global) ---
    def ouvrir_loupe(self):
        if not IS_WINDOWS:
            self._signaler("Loupe système : disponible uniquement sous Windows.")
            return
        try:
            subprocess.Popen(["magnify.exe"])
            self._signaler("Loupe Windows ouverte.")
        except Exception as e:
            self._signaler(f"Impossible d'ouvrir la loupe : {e}")

    def zoom_loupe_plus(self):
        self._envoyer_raccourci_systeme(['windows', '+'])

    def zoom_loupe_moins(self):
        self._envoyer_raccourci_systeme(['windows', '-'])

    # --- Clavier visuel ---
    def ouvrir_clavier_visuel(self):
        if not IS_WINDOWS:
            self._signaler("Clavier visuel : disponible uniquement sous Windows.")
            return
        try:
            subprocess.Popen(["osk.exe"])
            self._signaler("Clavier visuel ouvert.")
        except Exception as e:
            self._signaler(f"Impossible d'ouvrir le clavier visuel : {e}")

    # --- Narrateur Windows ---
    def basculer_narrateur(self):
        if not IS_WINDOWS:
            self._signaler("Narrateur : disponible uniquement sous Windows.")
            return
        self._envoyer_raccourci_systeme(['windows', 'ctrl', 'enter'])
        self._signaler("Bascule du Narrateur Windows demandée.")

    # --- Volume système ---
    def volume_plus(self):
        self._envoyer_touche_virtuelle(VK_VOLUME_UP)

    def volume_moins(self):
        self._envoyer_touche_virtuelle(VK_VOLUME_DOWN)

    def volume_muet(self):
        self._envoyer_touche_virtuelle(VK_VOLUME_MUTE)

    def _envoyer_touche_virtuelle(self, code_vk):
        if not IS_WINDOWS:
            self._signaler("Contrôle du volume : disponible uniquement sous Windows.")
            return
        try:
            ctypes.windll.user32.keybd_event(code_vk, 0, 0, 0)
            ctypes.windll.user32.keybd_event(code_vk, 0, KEYEVENTF_KEYUP, 0)
        except Exception as e:
            self._signaler(f"Erreur touche système : {e}")

    def _envoyer_raccourci_systeme(self, touches):
        if KEYBOARD_DISPONIBLE:
            try:
                keyboard.press_and_release('+'.join(touches))
            except Exception as e:
                self._signaler(f"Erreur raccourci : {e}")
        else:
            self._signaler("Module 'keyboard' requis pour ce raccourci.")

    # --- Synthèse vocale : lit à voix haute n'importe quel texte ---
    def lire_texte(self, texte):
        if not TTS_DISPONIBLE:
            self._signaler("Module 'pyttsx3' requis pour la lecture vocale.")
            return
        if not texte.strip():
            self._signaler("Rien à lire.")
            return

        def _lire():
            try:
                self.moteur_tts.say(texte)
                self.moteur_tts.runAndWait()
            except Exception as e:
                self._signaler(f"Erreur lecture vocale : {e}")

        threading.Thread(target=_lire, daemon=True).start()

    # --- Raccourcis clavier globaux (actifs sur tout l'ordinateur) ---
    def activer_raccourcis_globaux(self, callbacks):
        """callbacks : dict {combinaison_clavier: fonction_a_appeler}"""
        if not KEYBOARD_DISPONIBLE:
            self._signaler("Module 'keyboard' non installé : raccourcis globaux indisponibles.")
            return
        if self.hotkeys_actifs:
            return
        for combo, fonction in callbacks.items():
            hotkey_id = keyboard.add_hotkey(combo, fonction)
            self._hotkeys_ids.append(hotkey_id)
        self.hotkeys_actifs = True
        self._signaler("Raccourcis clavier globaux activés (fonctionnent même hors de l'app).")

    def desactiver_raccourcis_globaux(self):
        if not KEYBOARD_DISPONIBLE or not self.hotkeys_actifs:
            return
        for hotkey_id in self._hotkeys_ids:
            try:
                keyboard.remove_hotkey(hotkey_id)
            except Exception:
                pass
        self._hotkeys_ids.clear()
        self.hotkeys_actifs = False
        self._signaler("Raccourcis clavier globaux désactivés.")



# =========================================================================
# ACCESSIBILITE SYSTEME (agit sur tout l'ordinateur, pas que le logiciel)
# =========================================================================
class HIGHCONTRAST(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("dwFlags", ctypes.c_uint),
        ("lpszDefaultScheme", ctypes.c_wchar_p),
    ]


SPI_SETHIGHCONTRAST = 0x0043
HCF_HIGHCONTRASTON = 0x00000001
SPIF_UPDATEINIFILE = 0x01
SPIF_SENDCHANGE = 0x02

VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
KEYEVENTF_KEYUP = 0x0002



# =========================================================================
# ENREGISTREMENT MICRO + TRANSCRIPTION OPENAI (WHISPER)
# =========================================================================
class EnregistreurVocal:
    def __init__(self, on_transcription, on_erreur, on_statut):
        self.on_transcription = on_transcription
        self.on_erreur = on_erreur
        self.on_statut = on_statut
        self.enregistrement_en_cours = False
        self._frames = []
        self._stream = None
        self.client = None
        if OPENAI_DISPONIBLE:
            try:
                self.client = OpenAI(api_key=OPENAI_API_KEY)
            except Exception as e:
                self.on_erreur(f"Erreur d'initialisation OpenAI : {e}")

    def demarrer(self):
        if not AUDIO_DISPONIBLE:
            self.on_erreur("Modules 'sounddevice'/'numpy' requis pour utiliser le micro.")
            return
        if self.enregistrement_en_cours:
            return
        self._frames = []
        self.enregistrement_en_cours = True
        self.on_statut("🎙️ Enregistrement en cours...")

        def callback(indata, frames, time_info, status):
            self._frames.append(indata.copy())

        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=CANAUX,
                dtype='int16', callback=callback
            )
            self._stream.start()
        except Exception as e:
            self.enregistrement_en_cours = False
            self.on_erreur(f"Impossible d'accéder au micro : {e}")

    def arreter_et_transcrire(self):
        if not self.enregistrement_en_cours:
            return
        self.enregistrement_en_cours = False
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass
        self.on_statut("⏳ Transcription en cours...")

        def traiter():
            global paroles_dites_TXT
            try:
                if not self._frames:
                    self.on_erreur("Aucun son n'a été capté.")
                    self.on_statut("Prêt.")
                    return

                audio = np.concatenate(self._frames, axis=0)
                chemin_wav = os.path.join(tempfile.gettempdir(), "enregistrement_temp.wav")
                with wave.open(chemin_wav, 'wb') as f:
                    f.setnchannels(CANAUX)
                    f.setsampwidth(2)  # int16 -> 2 octets
                    f.setframerate(SAMPLE_RATE)
                    f.writeframes(audio.tobytes())

                if not self.client:
                    self.on_erreur("Client OpenAI non configuré (vérifiez OPENAI_API_KEY).")
                    self.on_statut("Prêt.")
                    return

                with open(chemin_wav, "rb") as fichier_audio:
                    reponse = self.client.audio.transcriptions.create(
                        model="whisper-1",
                        file=fichier_audio,
                        language="fr"
                    )
                texte = reponse.text.strip()
                paroles_dites_TXT = texte

                ask = paroles_dites_TXT

                # Exécuter master.py
                prompt1 = "python master.py \"{a} (tell me when to navigate, search, scroll until finding, and the borders to overcome like re-capcha, ...)\"".format(a=ask)
    
                output1 = subprocess.check_output(prompt1, shell=True, text=True)
                print("prompt1 :", prompt1)
                print("output1 :", output1)

                # Exécuter slave.py avec la sortie de master.py
                prompt2 = "python slave.py \"{o}. Puis {p}\"".format(o=output1,p=ask)
                output2 = subprocess.check_output(prompt2, shell=True, text=True)
                print("output2 :", output2)

    

                self.on_transcription(texte)
                self.on_statut("Prêt.")
            except Exception as e:
                self.on_erreur(f"Erreur de transcription : {e}")
                self.on_statut("Prêt.")

        threading.Thread(target=traiter, daemon=True).start()


# =========================================================================
# INTERFACE GRAPHIQUE
# =========================================================================
class LogicielAccessible(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("HERVÉ")
        self.geometry("1000x680")
        self.minsize(880, 600)
        self.configure(bg=BLEU_TRES_CLAIR)

        self.taille_police = 14
        self.micro_actif = False

        self.acces = AccessibiliteSysteme(on_message=self._afficher_message_statut)
        self.enregistreur = EnregistreurVocal(
            on_transcription=self._sur_transcription,
            on_erreur=self._afficher_erreur,
            on_statut=self._afficher_message_statut
        )

        self._construire_entete()
        self._construire_corps()
        self._construire_pied_de_page()

        self.protocol("WM_DELETE_WINDOW", self._a_la_fermeture)

    # ----------------- EN-TETE DECORATIF (bleu, purement esthétique) -----------------
    def _construire_entete(self):
        entete = tk.Canvas(self, height=100, bg=BLEU_NUIT, highlightthickness=0)
        entete.pack(fill="x", side="top")

        largeur = 1400
        for i in range(0, largeur, 3):
            t = i / largeur
            r = int(int(BLEU_NUIT[1:3], 16) * (1 - t) + int(BLEU_PRINCIPAL[1:3], 16) * t)
            g = int(int(BLEU_NUIT[3:5], 16) * (1 - t) + int(BLEU_PRINCIPAL[3:5], 16) * t)
            b = int(int(BLEU_NUIT[5:7], 16) * (1 - t) + int(BLEU_PRINCIPAL[5:7], 16) * t)
            couleur = f"#{r:02x}{g:02x}{b:02x}"
            entete.create_line(i, 0, i, 100, fill=couleur)

        # petites décorations "inutiles" mais jolies : cercles et étoiles
        for x, y, rayon in [(40, 22, 6), (75, 60, 4), (1330, 28, 8),
                             (1300, 65, 5), (1360, 48, 3), (20, 75, 3)]:
            entete.create_oval(x - rayon, y - rayon, x + rayon, y + rayon,
                                fill=BLEU_CLAIR, outline="")
        entete.create_text(500, 40, text="HERVÉ",
                            font=("Segoe UI", 24, "bold"), fill=BLANC)
        entete.create_text(500, 75, text="Aide au control de l'ordinateur  •  Agent IA  •  Assistance",
                            font=("Segoe UI", 11, "italic"), fill=BLEU_CLAIR)

    # ----------------- CORPS PRINCIPAL -----------------
    def _construire_corps(self):
        conteneur = tk.Frame(self, bg=BLEU_TRES_CLAIR)
        conteneur.pack(fill="both", expand=True, padx=18, pady=14)

        # --- Colonne gauche : zone de texte + micro ---
        colonne_gauche = tk.Frame(conteneur, bg=BLANC,
                                   highlightbackground=BLEU_PRINCIPAL, highlightthickness=3)
        colonne_gauche.pack(side="left", fill="both", expand=True, padx=(0, 12))

        tk.Label(colonne_gauche, text="📝",
                 bg=BLANC, fg=BLEU_NUIT, font=("Segoe UI", 13, "bold"),
                 anchor="w").pack(fill="x", padx=16, pady=(14, 4))

        cadre_texte = tk.Frame(colonne_gauche, bg=BLANC)
        cadre_texte.pack(fill="both", expand=True, padx=16, pady=6)

        self.police_texte = tkfont.Font(family="Segoe UI", size=self.taille_police)
        self.zone_texte = tk.Text(
            cadre_texte, wrap="word", font=self.police_texte,
            bg=BLEU_TRES_CLAIR, fg=GRIS_TEXTE, relief="flat",
            insertbackground=BLEU_NUIT, padx=10, pady=10, height=14
        )
        barre_defilement = ttk.Scrollbar(cadre_texte, command=self.zone_texte.yview)
        self.zone_texte.configure(yscrollcommand=barre_defilement.set)
        self.zone_texte.pack(side="left", fill="both", expand=True)
        barre_defilement.pack(side="right", fill="y")
        self.zone_texte.insert("1.0", "Bienvenue ! Le texte transcrit ou affiché apparaîtra ici.")

        # --- Bouton micro, juste sous la zone de texte ---
        cadre_micro = tk.Frame(colonne_gauche, bg=BLANC)
        cadre_micro.pack(fill="x", padx=16, pady=(6, 16))

        self.bouton_micro = tk.Button(
            cadre_micro, text="🎙️  ALLUMER LE MICRO", font=("Segoe UI", 15, "bold"),
            bg=BLEU_PRINCIPAL, fg=BLANC, activebackground=BLEU_FONCE, activeforeground=BLANC,
            relief="flat", bd=0, padx=20, pady=16, cursor="hand2",
            command=self._basculer_micro
        )
        self.bouton_micro.pack(fill="x")

        self.bouton_lire = tk.Button(
            cadre_micro, text="🔊  Lire le texte à voix haute", font=("Segoe UI", 11, "bold"),
            bg=BLEU_CLAIR, fg=BLEU_NUIT, relief="flat", bd=0, padx=10, pady=10, cursor="hand2",
            command=lambda: self.acces.lire_texte(self.zone_texte.get("1.0", "end"))
        )
        self.bouton_lire.pack(fill="x", pady=(10, 0))

        # --- Colonne droite : panneau d'accessibilité ---
        self._construire_panneau_accessibilite(conteneur)

    # ----------------- PANNEAU ACCESSIBILITE -----------------
    def _construire_panneau_accessibilite(self, parent):
        panneau = tk.Frame(parent, bg=BLANC, width=300,
                            highlightbackground=BLEU_PRINCIPAL, highlightthickness=3)
        panneau.pack(side="right", fill="y")
        panneau.pack_propagate(False)

        tk.Label(panneau, text="On vous aime !", bg=BLANC, fg=BLEU_NUIT,
                 font=("Segoe UI", 15, "bold")).pack(pady=(16, 4), padx=16, anchor="w")
        tk.Label(panneau, text="Certaines options agissent aussi\nsur tout l'ordinateur.",
                 bg=BLANC, fg=BLEU_FONCE, font=("Segoe UI", 9, "italic"),
                 justify="left").pack(padx=16, anchor="w", pady=(0, 10))

        def bouton_panneau(texte, commande):
            b = tk.Button(panneau, text=texte, font=("Segoe UI", 10, "bold"),
                          bg=BLEU_TRES_CLAIR, fg=BLEU_NUIT, relief="flat",
                          anchor="w", padx=12, pady=10, cursor="hand2", command=commande)
            b.pack(fill="x", padx=16, pady=4)

        def bouton_panneau(texte, commande):
         b = tk.Button(
         panneau,
         text=texte,  # Texte peut contenir \n pour plusieurs lignes
         font=("Segoe UI", 10, "bold"),
         bg=BLEU_TRES_CLAIR,
         fg=BLEU_NUIT,
         activebackground="#1a73e8",  # Couleur pendant le clic
         activeforeground="white",    # Texte pendant le clic
         relief="flat",
         anchor="w",
         padx=12,
         pady=10,
         cursor="hand2",
         wraplength=200,  # Force le texte à s'adapter sur plusieurs lignes
         command=commande
         )
         b.pack(fill="x", padx=16, pady=4)
         return b

        tk.Label(panneau, text="Taille du texte", bg=BLANC, fg=GRIS_TEXTE,
                 font=("Segoe UI", 10, "bold")).pack(padx=16, pady=(10, 0), anchor="w")
        cadre_police = tk.Frame(panneau, bg=BLANC)
        cadre_police.pack(fill="x", padx=16, pady=6)
        tk.Button(cadre_police, text="A-", font=("Segoe UI", 12, "bold"), bg=BLEU_CLAIR,
                  fg=BLEU_NUIT, relief="flat", width=4,
                  command=lambda: self._changer_taille_police(-2)).pack(side="left", padx=(0, 6))
        tk.Button(cadre_police, text="A+", font=("Segoe UI", 12, "bold"), bg=BLEU_CLAIR,
                  fg=BLEU_NUIT, relief="flat", width=4,
                  command=lambda: self._changer_taille_police(2)).pack(side="left")


        beauty_list = ["La vieillesse n’est pas une maladie, c’est une étape de la vie à vivre avec dignité.","On ne mesure pas la force d’une personne à son âge, mais à sa capacité à se relever après chaque chute.","Le handicap n’est pas une limite, mais une autre façon de voir le monde.","La vieillesse est un privilège denied à beaucoup.","Chaque jour est une nouvelle victoire, même si c’est juste pour se lever le matin.","La sagesse vient avec l’âge, et c’est une richesse inestimable.","On ne devient pas plus faible en vieillissant, on devient plus sage.","La vie n’est pas faite pour être facile, mais pour être vécue avec courage.","Le temps peut ralentir nos pas, mais il ne peut pas arrêter notre esprit.","La vieillesse est comme un bon vin : plus elle est âgée, plus elle a de valeur.","Accepter son corps ou ses limites, c’est déjà un pas vers le bonheur.","Le bonheur ne dépend pas de ce qu’on a, mais de ce qu’on fait avec ce qu’on a.","La vie est une aventure, même avec un fauteuil roulant ou des béquilles.","On ne choisit pas toujours les cartes qu’on nous distribue, mais on peut choisir comment les jouer.","La vieillesse n’est pas une fin, mais une nouvelle page à écrire.","Le bonheur est une question d’attitude, pas de capacité physique.","Chaque jour est une nouvelle chance de sourire, même si c’est pour soi-même.","La vie est trop courte pour se plaindre de ce qu’on ne peut pas changer.","Le sourire est le meilleur accessoire, quel que soit l’âge ou le handicap.","La vie est une danse, même si on doit l’adapter à son rythme.","Les rides sont les cicatrices de la vie, et elles racontent une belle histoire.","La vieillesse est le seul moyen de vivre longtemps.","Les personnes âgées sont comme les livres : plus on les lit, plus on en apprend.","La sagesse n’a pas d’âge, mais l’expérience la rend plus précieuse.","Les personnes âgées sont les gardiennes de la mémoire collective.","La vieillesse est une école où l’on apprend à apprécier les petites choses.","Les personnes âgées ont vu le monde changer, et elles ont survécu à tout.","La vieillesse est une preuve que l’on a vécu, et c’est déjà une réussite.","Les personnes âgées sont comme les étoiles : elles brillent même dans l’obscurité.","La vieillesse est une bénédiction, car elle permet de voir la vie sous un nouvel angle.","L’amour ne connaît ni l’âge ni les limites physiques.","Les personnes âgées ont encore beaucoup d’amour à donner.","L’amitié est un trésor, quel que soit l’âge ou le handicap.","Les personnes âgées sont des phares dans la vie de leurs proches.","L’amour est la seule chose qui ne vieillit pas.","Les personnes âgées ont des histoires à raconter, et elles méritent d’être écoutées.","L’amour est une force qui transcende toutes les limites.","Les personnes âgées sont des guides, même si on ne s’en rend pas toujours compte.","L’amour est la clé pour surmonter toutes les épreuves.","Les personnes âgées ont des leçons de vie à partager, et elles le font avec générosité.","La liberté n’a pas d’âge, et elle se cultive chaque jour.","Même avec un handicap, on peut être libre de choisir sa vie.","La vieillesse n’est pas une prison, mais une nouvelle façon de voir le monde.","L’autonomie est une question d’état d’esprit, pas de capacité physique.","On peut être libre même assis dans un fauteuil roulant.","La liberté, c’est de pouvoir choisir comment on veut vivre sa vie.","La vieillesse n’est pas une fin, mais une nouvelle aventure.","L’autonomie, c’est de pouvoir décider pour soi, même avec des limites.","La liberté, c’est de pouvoir rire, aimer et rêver, quel que soit son âge.","La vieillesse est une preuve que l’on a encore des rêves à réaliser.","Le bonheur est une question de choix, pas de circonstances.","On peut être heureux à 20 ans comme à 80 ans.","Le sourire est le langage universel de la joie.","Le bonheur ne dépend pas de ce qu’on a, mais de ce qu’on ressent.","La vie est trop courte pour ne pas en profiter, quel que soit son âge.","Le bonheur est une attitude, pas une destination.","On peut trouver la joie même dans les petits moments.","Le bonheur est une question de perspective.","La vie est une fête, même si on doit l’adapter à son rythme.","Le bonheur est une décision qu’on prend chaque jour.","La persévérance est la clé pour surmonter toutes les épreuves.","On ne abandonne pas, on s’adapte.","Chaque jour est une nouvelle chance de se battre.","La détermination est plus forte que les limites physiques.","On peut tout surmonter avec du courage et de la volonté.","La vie est une série d’obstacles, mais on peut les franchir un à un.","La persévérance est une force qui ne s’éteint jamais.","On ne choisit pas toujours les épreuves, mais on choisit comment les affronter.","La vie est une montagne, mais on peut toujours trouver un chemin.","La détermination est la clé pour réaliser ses rêves.","La gratitude est la clé pour voir la beauté dans chaque jour.","On a toujours quelque chose pour lequel être reconnaissant.","La vie est une bénédiction, même avec ses défis.","La gratitude transforme les épreuves en opportunités.","Chaque jour est une nouvelle chance de dire merci.","La vie est trop précieuse pour ne pas en être reconnaissant.","La gratitude est un remède contre les soucis.","On peut toujours trouver une raison de sourire.","La vie est une aventure, et chaque jour est une nouvelle page.","La gratitude est la clé pour apprécier ce qu’on a.","La créativité n’a pas d’âge, et elle peut s’exprimer de mille façons.","Même avec un handicap, on peut créer, inventer et rêver.","La vieillesse est une source d’inspiration pour l’art et la culture.","On peut toujours trouver une nouvelle passion, quel que soit son âge.","La créativité est une force qui ne s’éteint jamais.","La vie est une toile blanche, et on peut y peindre sa propre histoire.","Même avec des limites, on peut laisser une trace dans le monde.","La créativité est une façon de transcender les obstacles.","On peut toujours trouver une nouvelle façon de s’exprimer.","La vieillesse est une source d’inspiration pour les générations futures.","L’espoir est une lumière qui guide même dans les moments sombres.","Demain est une nouvelle page, et on peut toujours écrire une belle histoire.","L’espoir est une force qui ne s’éteint jamais.","La vie est une aventure, et chaque jour est une nouvelle chance.","L’espoir est la clé pour surmonter toutes les épreuves.","Même dans les moments difficiles, il y a toujours une raison d’espérer.","L’avenir est une promesse, et on peut toujours y croire.","L’espoir est une force qui nous pousse à avancer.","La vie est une série de défis, mais on peut toujours trouver une solution.","L’espoir est la clé pour voir la vie sous un angle positif.💙"]
        day_word_list = []
        for i in range(5):
          index_word = random.randint(2, 97)
          day_word_list.append(beauty_list[index_word])
        bouton_panneau(day_word_list[0],print("a"))
        bouton_panneau(day_word_list[1],print("a"))
        bouton_panneau(day_word_list[2],print("a"))
        bouton_panneau(day_word_list[3],print("a"))
        bouton_panneau(day_word_list[4],print("a"))
        bouton_panneau("🗣️  Narrateur Windows on/off", self.acces.basculer_narrateur)
        bouton_panneau("🔉  Volume -", self.acces.volume_moins)
        bouton_panneau("🔊  Volume +", self.acces.volume_plus)
        bouton_panneau("🔇  Muet", self.acces.volume_muet)
        bouton_panneau("🌓  Contraste élevé (système)", self.acces.basculer_contraste_eleve)

        aide = ("Ctrl+Alt+M : micro\n"
                "Ctrl+Alt+L : lire le texte\n"
                "Ctrl+Alt+C : contraste\n"
                "Ctrl+Alt+Haut/Bas : volume")
        tk.Label(panneau, text=aide, bg=BLEU_TRES_CLAIR, fg=BLEU_NUIT, font=("Consolas", 9),
                 justify="left", padx=10, pady=8).pack(fill="x", padx=16, pady=(4, 16))

    # ----------------- PIED DE PAGE / STATUT -----------------
    def _construire_pied_de_page(self):
        pied = tk.Frame(self, bg=BLEU_FONCE, height=32)
        pied.pack(fill="x", side="bottom")
        self.label_statut = tk.Label(pied, text="Prêt.", bg=BLEU_FONCE, fg=BLANC,
                                      font=("Segoe UI", 10, "bold"), anchor="w", padx=14)
        self.label_statut.pack(fill="x")

    # ----------------- LOGIQUE -----------------
    def _basculer_micro(self):
        if not self.micro_actif:
            self.micro_actif = True
            self.bouton_micro.config(text="⏹️  ARRÊTER LE MICRO", bg="#C0392B",
                                      activebackground="#96281B")
            self.enregistreur.demarrer()
        else:
            self.micro_actif = False
            self.bouton_micro.config(text="🎙️  ALLUMER LE MICRO", bg=BLEU_PRINCIPAL,
                                      activebackground=BLEU_FONCE)
            self.enregistreur.arreter_et_transcrire()

    def _sur_transcription(self, texte):
        global paroles_dites_TXT
        paroles_dites_TXT = texte
        self.zone_texte.delete("1.0", "end")
        self.zone_texte.insert("1.0", texte)
        self._afficher_message_statut(f"Transcription reçue ({len(texte)} caractères).")

    def afficher_texte(self, texte):
        """A utiliser depuis votre code pour afficher n'importe quel texte
        dans la zone de texte de l'interface."""
        self.zone_texte.delete("1.0", "end")
        self.zone_texte.insert("1.0", texte)

    def _changer_taille_police(self, delta):
        self.taille_police = max(8, min(40, self.taille_police + delta))
        self.police_texte.configure(size=self.taille_police)

    def _basculer_raccourcis_globaux(self):
        if self.var_raccourcis.get():
            callbacks = {
                'ctrl+alt+m': lambda: self.after(0, self._basculer_micro),
                'ctrl+alt+l': lambda: self.after(
                    0, lambda: self.acces.lire_texte(self.zone_texte.get("1.0", "end"))),
                'ctrl+alt+c': lambda: self.after(0, self.acces.basculer_contraste_eleve),
                'ctrl+alt+up': lambda: self.after(0, self.acces.volume_plus),
                'ctrl+alt+down': lambda: self.after(0, self.acces.volume_moins),
            }
            self.acces.activer_raccourcis_globaux(callbacks)
        else:
            self.acces.desactiver_raccourcis_globaux()

    def _afficher_message_statut(self, message):
        self.label_statut.config(text=message)

    def _afficher_erreur(self, message):
        self.label_statut.config(text=f"⚠️ {message}")
        messagebox.showerror("Erreur", message)

    def _a_la_fermeture(self):
        self.acces.desactiver_raccourcis_globaux()
        self.destroy()


# =========================================================================
# POINT D'ENTREE
# =========================================================================
if __name__ == "__main__":
    if not OPENAI_DISPONIBLE:
        print("⚠️  Module 'openai' manquant (pip install openai).")
    if not AUDIO_DISPONIBLE:
        print("⚠️  Modules 'sounddevice'/'numpy' manquants (pip install sounddevice numpy).")
    if not KEYBOARD_DISPONIBLE:
        print("⚠️  Module 'keyboard' manquant : raccourcis globaux indisponibles.")
    if not TTS_DISPONIBLE:
        print("⚠️  Module 'pyttsx3' manquant : lecture vocale indisponible.")

    app = LogicielAccessible()
    app.mainloop()
