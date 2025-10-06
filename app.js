const loadNewsBtn = document.getElementById('loadNews');
const newsContainer = document.getElementById('newsContainer');
const loader = document.getElementById('loader');

loadNewsBtn.addEventListener('click', () => {
  newsContainer.innerHTML = '';
  loader.style.display = 'block';

  fetch("https://cors-anywhere.herokuapp.com/https://n8n-8ush.onrender.com/webhook/feed", {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({ email: "suriayrus030@gmail.com" })
  })
  .then(res => res.json())
  .then(data => {
    loader.style.display = 'none';
    console.log('Response:', data);

    const output = data.output;
    if (!output) {
      newsContainer.innerHTML = '<p>No news found.</p>';
      return;
    }

    let i = 1;
    let found = false;

    while (output[`headline${i}`]) {
      found = true;
      const card = document.createElement('div');
      card.className = 'newsCard';
      card.innerHTML = `
        <img src="${output[`image${i}`]}" alt="News Image">
        <h3>${output[`headline${i}`]}</h3>
        <p>${output[`news${i}`]}</p>
        <a href="${output[`link${i}`]}" target="_blank">Read more</a>
        <p style="font-size:0.8rem;color:#ccc;">Categories: ${output[`categories${i}`]}</p>
      `;
      newsContainer.appendChild(card);
      i++;
    }

    if (!found) {
      newsContainer.innerHTML = '<p>No news found.</p>';
    }
  })
  .catch(err => {
    loader.style.display = 'none';
    newsContainer.innerHTML = '<p>Error loading news ❌</p>';
    console.error('Fetch error details:', err);
  });
});
