import subprocess

ask = input("Tell me what you want I do? ")
satisfied = False

while not satisfied:
    # Exécuter master.py
    prompt1 = "python master.py \"{a} (tell me when to navigate, search, scroll until finding, and the borders to overcome like re-capcha, ...)\"".format(a=ask)
    
    output1 = subprocess.check_output(prompt1, shell=True, text=True)
    print("prompt1 :", prompt1)
    print("output1 :", output1)

    # Exécuter slave.py avec la sortie de master.py
    prompt2 = "python slave.py \"{o}. Puis {p}\"".format(o=output1,p=ask)
    output2 = subprocess.check_output(prompt2, shell=True, text=True)
    print("output2 :", output2)

    ok = input("Are you satisfied ? If yes : say 'Yes' ")
    if ok.strip().lower() == "yes":
        satisfied = True
