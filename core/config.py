"""
Configuration -- seuils et parametres ajustables
====================================================

Centralise les valeurs qu'on veut pouvoir changer sans toucher au code
metier. Pour l'instant : le seuil de trim automatique (prise de profit
partielle). D'autres constantes (PILLAR_WEIGHTS, MOMENTUM_POINTS,
TRANCHE_WEIGHTS... actuellement dans composite_scorer.py) pourront migrer
ici plus tard si besoin -- pas fait maintenant pour rester focalise sur
ce qui est utile immediatement (regle de trim).
"""

# Seuil de gain (par rapport au prix d'entree) au-dela duquel une position
# est ramenee a son poids cible normal (trim partiel, PAS une sortie
# complete -- decision validee avec Thomas). 1.0 = +100%.
GAIN_THRESHOLD_FOR_TRIM = 1.0

# Cycle de planification mensuelle (etape 3) -- pour une position EN BAISSE
# (gain < 0), le score actualise (Piliers 2/3, via portfolio_review.py) est
# juge "degrade" si la chute par rapport au score d'entree depasse ce seuil.
# Cloture automatique SEULEMENT si Piliers 2/3 degrades ET Pilier 1 sans
# achat d'initie recent (les deux negatifs -- decision validee avec Thomas).
# Sinon, alerte seule -- jamais de cloture sur un seul des deux signaux.
FUNDAMENTALS_DEGRADATION_THRESHOLD = -10.0  # points de score perdus depuis l'entree
INSIDER_ACTIVITY_LOOKBACK_DAYS = 60  # fenetre de verification du Pilier 1 reactualise

# Empeche de racheter immediatement un titre tout juste sorti du portefeuille
# (ex. cloture automatique de position_health.py) -- evite un aller-retour
# vente/rachat inutile dans le meme cycle si les fenetres du Pilier 1 (etape 3)
# et du screening complet (etape 4) different legerement. Decision validee
# avec Thomas suite a un cas reel observe (SMMT vendue puis rachetee le
# meme cycle).
REBUY_COOLDOWN_DAYS = 14