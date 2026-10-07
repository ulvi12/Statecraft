# Statecraft

Statecraft is for students and anyone curious about regional economies. Create
fictional countries by combining US states, then compare their GDP, population,
GDP per person, and industry mix using official Bureau of Economic Analysis (BEA)
data. Gemini chooses the tools, and Python performs the calculations.

## Tools

- **`fetch_state_economies`** — Fetches GDP, population, and industry data for selected states using the latest complete annual BEA reporting year.
- **`assemble_country`** — Creates, updates, or renames a fictional country and calculates its combined economy and industry shares.
- **`compare_countries`** — Compares created countries by GDP, population, GDP per person, or industry mix and identifies shared states.

## How to use

Open the deployed site and describe the country you want to create in the chat.
You can also click states on the US map, enter a name, and select **Create country**.
Ask follow-up questions to change borders, rename countries, or compare them.
Country cards and expandable tables show the results. Under **Tool calls and raw
data** beneath an answer, expand a tool entry to inspect its name, arguments, and result.
Use **New chat** to start a separate world.

Try these three example queries:

1. "Create Pacifica from California, Oregon, and Washington. Show its GDP, population, and GDP per person."
2. "Create Pacifica from California, Oregon, and Washington, and Lone Star from Texas. Compare their total GDP."
3. "Create Pacifica from California, Oregon, and Washington, and Lone Star from Texas. Compare their industry mix and explain which industries drive each economy."

After creating Pacifica, try: "Add Nevada to Pacifica. Did its GDP per person rise?"
