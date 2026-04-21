import { Button } from '@chakra-ui/react'

interface IScrollToTopProps {
  onClickScroll: () => void
}

const ScrollToTop = ({ onClickScroll }: IScrollToTopProps) => {
  const scrollClickHandler = () => {
    onClickScroll()
  }
  return (
    <Button
      onClick={scrollClickHandler}
      position="fixed"
      right="10"
      bottom="20"
      bgColor="primary"
      width="12"
      height="12"
    >
      <svg
        className="w-4 h-4"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
        xmlns="http://www.w3.org/2000/svg"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth="2"
          d="M5 10l7-7m0 0l7 7m-7-7v18"
        ></path>
      </svg>
    </Button>
  )
}

export default ScrollToTop
