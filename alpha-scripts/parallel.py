import math
from tqdm import tqdm
from joblib import Parallel, delayed

# dmoe https://www.youtube.com/watch?v=oJLaA7-i3nI

results = [math.factorial(x) for x in tqdm(range(15000))]
results = Parallel(n_jobs=-1)(delayed(math.factorial)(x) for x in tqdm(range(15000)))