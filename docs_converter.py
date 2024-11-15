import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md
import os
from urllib.parse import urljoin, urlparse, unquote
import time
import re
import argparse
from collections import deque
import google.generativeai as genai
import json
from jinja2 import Environment, FileSystemLoader
from pathlib import Path

class DocsConverter:
    def __init__(self, base_url, domain=None, max_depth=None, gemini_api_key=None, session=None):
        self.base_url = base_url.rstrip('/')
        self.domain = domain or urlparse(base_url).netloc
        self.max_depth = max_depth
        self.visited_urls = set()
        self.session = session or requests.Session()
        self.exclude_selectors = [
            'nav', 'header', 'footer', 
            '.sidebar', '.navigation', '.menu',
            '#sidebar', '#navigation', '#menu',
            '.search', '#search',
            '.toolbar', '#toolbar'
        ]
        
        # Initialize Jinja environment
        templates_dir = Path(__file__).parent / 'templates' / 'prompts'
        self.jinja_env = Environment(loader=FileSystemLoader(templates_dir))
        
        # Initialize Gemini if API key is provided
        if gemini_api_key:
            genai.configure(api_key=gemini_api_key)
            self.model = genai.GenerativeModel('gemini-1.5-flash-002')
        else:
            self.model = None
        
    def get_url_depth(self, url):
        """Calculate the depth of a URL relative to base_url."""
        base_path = urlparse(self.base_url).path.rstrip('/').split('/')
        url_path = urlparse(url).path.rstrip('/').split('/')
        
        common_prefix_len = 0
        for bp, up in zip(base_path, url_path):
            if bp == up:
                common_prefix_len += 1
            else:
                break
                
        return len(url_path) - common_prefix_len
        
    def is_valid_url(self, url):
        """Check if URL is valid and belongs to the same documentation domain."""
        parsed = urlparse(url)
        
        if not all([parsed.scheme, parsed.netloc]):
            return False
            
        if self.domain not in parsed.netloc:
            return False
            
        if self.max_depth is not None:
            depth = self.get_url_depth(url)
            if depth > self.max_depth:
                return False
        
        if any(ext in url.lower() for ext in [
            '.png', '.jpg', '.jpeg', '.gif', '.css', '.js', 
            '.svg', '.ico', '.woff', '.woff2', '.ttf', '.eot'
        ]):
            return False
            
        if '#' in url or 'mailto:' in url.lower():
            return False
            
        if not url.startswith(self.base_url):
            return False
            
        return True
    
    def get_page_content(self, url):
        """Fetch and parse page content."""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            response = self.session.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            return BeautifulSoup(response.text, 'html.parser')
        except Exception as e:
            print(f"Error fetching {url}: {str(e)}")
            return None

    def normalize_url(self, url):
        """Normalize URL by removing trailing slashes and fragments."""
        parsed = urlparse(url)
        normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        if parsed.query:
            normalized += f"?{parsed.query}"
        return normalized

    def extract_links(self, soup, current_url):
        """Extract valid links from page."""
        links = set()
        seen_paths = set()
        
        for a in soup.find_all(['a']):
            if not a.get('href'):
                continue
                
            href = a['href']
            
            if href.startswith('#'):
                continue
                
            full_url = urljoin(current_url, href)
            normalized_url = self.normalize_url(full_url)
            
            parsed = urlparse(normalized_url)
            if parsed.path in seen_paths:
                continue
                
            seen_paths.add(parsed.path)
            
            if self.is_valid_url(normalized_url) and normalized_url not in self.visited_urls:
                links.add(normalized_url)
                
        return links

    def clean_content(self, soup):
        """Clean the HTML content before conversion."""
        for selector in self.exclude_selectors:
            for element in soup.select(selector):
                element.decompose()
            
        for element in soup.find_all(['script', 'style', 'iframe', 'noscript']):
            element.decompose()
            
        main_content = (
            soup.find('main') or 
            soup.find('article') or 
            soup.find('div', {'class': ['content', 'main', 'document', 'documentation']}) or 
            soup.find('div', {'role': 'main'}) or
            soup
        )
        
        for element in main_content.find_all():
            if len(element.get_text(strip=True)) == 0:
                element.decompose()
                
        return main_content

    def get_page_title(self, soup, url):
        """Extract the page title using multiple fallback methods."""
        for selector in [
            'h1',
            'title',
            'h2',
            ['meta', {'property': 'og:title'}],
            ['meta', {'name': 'title'}]
        ]:
            if isinstance(selector, list):
                element = soup.find(selector[0], selector[1])
                title = element.get('content') if element else None
            else:
                element = soup.find(selector)
                title = element.get_text(strip=True) if element else None
                
            if title:
                return title
                
        path = urlparse(url).path.rstrip('/')
        if path:
            return unquote(path.split('/')[-1].replace('-', ' ').replace('_', ' ').title())
        return 'Documentation'

    def clean_markdown_with_gemini(self, content: str) -> str:
        """Use Gemini to clean and structure the markdown content."""
        if not self.model:
            return content
        
        # Only process content if it's not too large
        if len(content) > 30000:  # Gemini has a context limit
            print("[yellow]Content too large for Gemini, returning original[/yellow]")
            return content
            
        try:
            # Load and render the prompt template
            template = self.jinja_env.get_template('technical_docs_converter.jinja')
            prompt = template.render(content=content)
            
            # Generate content using Gemini
            response = self.model.generate_content(prompt)
            return response.text if response.text else content
        except Exception as e:
            print(f"[yellow]Gemini processing error: {e}[/yellow]")
            return content

    def post_process_markdown(self, content, generate_toc=False):
        """Post-process the markdown content to improve formatting and readability."""
        if not content:
            return content

        # Fix escaped characters
        content = re.sub(r'\\\-', '-', content)  # Fix escaped hyphens
        content = re.sub(r'\\\|', '|', content)  # Fix escaped pipes in tables
        content = re.sub(r'\\([#\[\]\(\)\*\_\~])', r'\1', content)  # Fix other common escaped characters
        
        # Normalize section spacing (ensure exactly two newlines between sections)
        content = re.sub(r'\n{3,}', '\n\n', content)
        
        # Fix link formatting
        content = re.sub(r'\[([^\]]+)\]\(([^\)]+)\)', lambda m: f'[{m.group(1).strip()}](#)', content)  # Sanitize links
        
        # Fix code block formatting
        content = re.sub(r'```\s*(\w+)\s*\n', r'```\1\n', content)  # Normalize language tags
        content = re.sub(r'```\n\n+', '```\n', content)  # Remove extra newlines after opening
        content = re.sub(r'\n\n+```', '\n```', content)  # Remove extra newlines before closing
        
        # Fix list formatting
        content = re.sub(r'^(\s*[-\*\+])\s+', r'\1 ', content, flags=re.MULTILINE)  # Normalize list item spacing
        content = re.sub(r'(\n\s*[-\*\+] [^\n]+)(\n\s*[-\*\+])', r'\1\n\2', content)  # Add newline between items
        
        # Fix table formatting
        content = re.sub(r'\|\s+\|', '|', content)  # Remove extra spaces in empty cells
        content = re.sub(r'\s+\|', ' |', content)   # Normalize spacing before pipes
        content = re.sub(r'\|\s+', '| ', content)   # Normalize spacing after pipes
        
        # Fix heading formatting
        content = re.sub(r'^(#+)([^#\s])', r'\1 \2', content, flags=re.MULTILINE)  # Ensure space after #
        content = re.sub(r'^(#+\s.*?)#+\s*$', r'\1', content, flags=re.MULTILINE)  # Remove trailing #
        
        if generate_toc:
            # Generate table of contents
            headers = []
            for line in content.split('\n'):
                if line.startswith('#'):
                    level = len(re.match(r'^#+', line).group())
                    title = line.lstrip('#').strip()
                    if level <= 3:  # Only include up to H3 in TOC
                        anchor = re.sub(r'[^\w\- ]', '', title.lower()).replace(' ', '-')
                        headers.append((level, title, anchor))
            
            if headers:
                toc = ['# Table of Contents\n']
                for level, title, anchor in headers:
                    indent = '  ' * (level - 1)
                    toc.append(f'{indent}- [{title}](#{anchor})')
                
                # Insert TOC after the first heading
                first_heading_end = content.find('\n', content.find('#'))
                if first_heading_end != -1:
                    content = content[:first_heading_end + 1] + '\n' + '\n'.join(toc) + '\n\n' + content[first_heading_end + 1:]
        
        return content.strip()

    def convert_page(self, url, depth=0):
        """Convert a single page to markdown."""
        print(f"{'  ' * depth}Converting: {url}")
        
        # Use session for connection pooling
        soup = self.get_page_content(url)
        if not soup:
            return set()

        # Process content in parallel where possible
        title_text = self.get_page_title(soup, url)
        content = self.clean_content(soup)
        links = self.extract_links(soup, url)
        
        # Generate markdown
        header_prefix = '#' * (depth + 1) if depth < 6 else '######'
        raw_markdown = f"{header_prefix} {title_text}\n\nSource: {url}\n\n{md(str(content))}\n\n---\n\n"
        
        # Clean with Gemini if available (batch process to reduce API calls)
        if self.model and len(raw_markdown) < 30000:  # Only process if content isn't too large
            try:
                markdown_content = self.clean_markdown_with_gemini(raw_markdown)
            except Exception as e:
                print(f"[yellow]Using raw markdown due to Gemini error: {e}[/yellow]")
                markdown_content = raw_markdown
        else:
            markdown_content = raw_markdown
        
        # Batch write to file
        with open(f"{self.domain}_docs.md", 'a', encoding='utf-8') as f:
            f.write(markdown_content)
        
        return links

    def convert_all_docs(self):
        """Convert all documentation pages to markdown using BFS traversal with optimizations."""
        print(f"Starting documentation conversion from {self.base_url}")
        print(f"Gemini API {'enabled' if self.model else 'disabled'} for content cleanup")
        
        # Initialize output file
        with open(f"{self.domain}_docs.md", 'w', encoding='utf-8') as f:
            f.write(f"# {self.domain} Documentation\n\n")
            f.write(f"Generated from: {self.base_url}\n\n---\n\n")
        
        # Use deque for efficient queue operations
        queue = deque([(self.base_url, 0)])
        self.visited_urls = set()  # Use set for O(1) lookups
        total_pages = 0
        batch_size = 10  # Process pages in batches
        
        try:
            while queue:
                # Process pages in batches for better performance
                batch = []
                while queue and len(batch) < batch_size:
                    url, depth = queue.popleft()
                    normalized_url = self.normalize_url(url)
                    if normalized_url not in self.visited_urls:
                        batch.append((normalized_url, depth))
                        self.visited_urls.add(normalized_url)
                
                # Process batch
                for url, depth in batch:
                    new_urls = self.convert_page(url, depth)
                    
                    # Add new URLs to queue
                    for new_url in new_urls:
                        if new_url not in self.visited_urls:
                            queue.append((new_url, depth + 1))
                    
                    total_pages += 1
                    
                # Small delay between batches to be nice to the server
                if batch:
                    time.sleep(1)
            
            print(f"\nConversion complete!")
            print(f"Total pages processed: {total_pages}")
            print(f"Documentation has been saved to: {self.domain}_docs.md")
            
        except KeyboardInterrupt:
            print("\nConversion interrupted by user")
            print(f"Partial documentation saved to: {self.domain}_docs.md")
            print(f"Pages processed: {total_pages}")

    def convert(self):
        """Convert documentation to markdown with linked pages."""
        try:
            print(f"\nStarting conversion of {self.base_url}")
            queue = deque([(self.base_url, 0)])
            markdown_sections = []
            
            while queue:
                url, depth = queue.popleft()
                normalized_url = self.normalize_url(url)
                
                if normalized_url in self.visited_urls:
                    continue
                    
                print(f"\nProcessing page: {url}")
                self.visited_urls.add(normalized_url)
                soup = self.get_page_content(url)
                
                if not soup:
                    print(f"Failed to get content for {url}")
                    continue
                
                # Clean content by removing navigation elements
                print("Cleaning content...")
                for selector in self.exclude_selectors:
                    for element in soup.select(selector):
                        element.decompose()
                
                # Find main content
                main_content = (
                    soup.find('main') or 
                    soup.find('article') or 
                    soup.find('div', {'class': ['content', 'main', 'document', 'documentation']}) or 
                    soup.find('div', {'role': 'main'}) or
                    soup
                )
                
                # Convert main content to markdown
                print("Converting to markdown...")
                content = md(str(main_content))
                
                if not content:
                    print("No content after conversion")
                    continue
                
                # Post-process markdown
                print("Post-processing markdown...")
                content = self.post_process_markdown(content)
                
                # Add page title and source
                title = self.get_page_title(soup, url) or url
                section = f"\n## {title}\n\nSource: {url}\n\n{content}\n\n---\n"
                markdown_sections.append(section)
                
                # Extract and process links if within depth limit
                if self.max_depth is None or depth < self.max_depth:
                    print(f"Extracting links from {url}")
                    links = self.extract_links(soup, url)
                    for link in links:
                        if link not in self.visited_urls:
                            print(f"Found new link: {link}")
                            queue.append((link, depth + 1))
                
                # Small delay between pages to be nice to the server
                time.sleep(0.5)
            
            if not markdown_sections:
                print("No content was converted")
                return None
            
            # Combine all sections
            print("\nCombining all sections...")
            final_content = f"# {self.domain} Documentation\n\nGenerated from: {self.base_url}\n\n---\n\n"
            final_content += "\n".join(markdown_sections)
            
            # Use Gemini if available
            if self.model:
                print("\nUsing Gemini for enhancement...")
                final_content = self.clean_markdown_with_gemini(final_content)
            
            print(f"\nConversion complete!")
            print(f"Pages processed: {len(markdown_sections)}")
            print(f"Total content length: {len(final_content)}")
            
            return final_content
            
        except Exception as e:
            print(f"\nError during conversion: {str(e)}")
            import traceback
            print(traceback.format_exc())
            return None

def main():
    parser = argparse.ArgumentParser(description='Convert documentation website to markdown')
    parser.add_argument('url', help='Base URL of the documentation (e.g., https://docs.example.com/)')
    parser.add_argument('--max-depth', '-d', type=int, help='Maximum depth of sub-pages to crawl (default: no limit)')
    parser.add_argument('--gemini-key', '-g', help='Google Gemini API key for content cleanup')
    args = parser.parse_args()
    
    converter = DocsConverter(
        args.url, 
        max_depth=args.max_depth,
        gemini_api_key=args.gemini_key
    )
    converter.convert_all_docs()

if __name__ == "__main__":
    main()